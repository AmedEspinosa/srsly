"""The ``clarify-json`` question protocol.

Ported from ``~/.config/nvim/lua/custom/copilot_questions.lua`` so the
orchestrator speaks exactly the protocol the existing Neovim workflow already
produces. Two parsers, tried in order:

1. A fenced ```clarify-json``` block — the structured, primary format.
2. The legacy "Clarifying questions" markdown block, kept for prompts that
   predate the structured format.

Normalisation matches the Lua field-for-field (``id`` defaulting to ``q<N>``,
``type`` to ``text``, ``allow_custom`` to true unless explicitly false), so a
response that worked in Neovim parses identically here.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

FENCE_PATTERN = re.compile(r"```clarify-json\s*\n(.*?)\n```", re.DOTALL)
LEGACY_HEADING = re.compile(r"clarifying questions", re.IGNORECASE)
LEGACY_NUMBERED = re.compile(r"^\s*\d+\.\s+(.+)$")
LEGACY_OPTION = re.compile(r"^\s*-\s+(.+)$")
LEGACY_RULE = re.compile(r"^\s*---")

QUESTION_TYPES = {"text", "single_select", "multi_select"}


@dataclass
class Question:
    id: str
    text: str
    type: str = "text"
    options: list[str] = field(default_factory=list)
    allow_custom: bool = True
    placeholder: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "text": self.text,
            "type": self.type,
            "options": list(self.options),
            "allow_custom": self.allow_custom,
            "placeholder": self.placeholder,
        }


@dataclass
class ClarifyPayload:
    ready: bool = False
    questions: list[Question] = field(default_factory=list)
    version: int = 1


def _normalize_question(raw: object, index: int) -> Question | None:
    if not isinstance(raw, dict):
        return None

    text = str(raw.get("text") or "")
    qid = str(raw.get("id") or f"q{index + 1}")

    qtype = str(raw.get("type") or "text")
    if qtype not in QUESTION_TYPES:
        qtype = "text"

    raw_options = raw.get("options")
    options = [str(o) for o in raw_options] if isinstance(raw_options, list) else []

    # The Lua defaults allow_custom to true unless it is explicitly false.
    allow_custom = raw.get("allow_custom") is not False

    return Question(
        id=qid,
        text=text,
        type=qtype,
        options=options,
        allow_custom=allow_custom,
        placeholder=str(raw.get("placeholder") or ""),
    )


def parse_payload(text: str) -> ClarifyPayload | None:
    """Parse a fenced ``clarify-json`` block. Returns None when absent/invalid."""
    match = FENCE_PATTERN.search(text or "")
    if not match:
        return None

    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None

    if not isinstance(data, dict) or not isinstance(data.get("questions"), list):
        return None

    questions: list[Question] = []
    for index, raw in enumerate(data["questions"]):
        question = _normalize_question(raw, index)
        if question is not None:
            questions.append(question)

    return ClarifyPayload(
        ready=data.get("ready") is True,
        questions=questions,
        version=int(data.get("v") or 1),
    )


def parse_questions_legacy(text: str) -> ClarifyPayload | None:
    """Parse the older "Clarifying questions" markdown block."""
    questions: list[Question] = []
    current: Question | None = None
    in_block = False

    for line in (text or "").splitlines():
        if not line.strip():
            continue
        if not in_block:
            if LEGACY_HEADING.search(line):
                in_block = True
            continue

        if LEGACY_RULE.match(line):
            break

        numbered = LEGACY_NUMBERED.match(line)
        if numbered:
            current = Question(id=f"q{len(questions) + 1}", text=numbered.group(1).strip())
            questions.append(current)
            continue

        if current is not None:
            option = LEGACY_OPTION.match(line)
            if option:
                current.options.append(option.group(1).strip())
                current.type = "single_select"

    if not questions:
        return None
    return ClarifyPayload(ready=False, questions=questions, version=0)


def parse_response(text: str) -> ClarifyPayload | None:
    """Structured format first, legacy markdown as a fallback."""
    return parse_payload(text) or parse_questions_legacy(text)


def strip_clarify_block(text: str) -> str:
    """Remove any clarify-json fence, leaving the prose around it."""
    return FENCE_PATTERN.sub("", text or "").strip()


def format_answers(questions: list[Question], answers: dict[str, object]) -> str:
    """Render answers back to the model.

    Mirrors ``format_answers_for_llm`` in the Lua, including the "(no
    preference)" placeholder and the closing instruction, so the model sees the
    turn shape it was trained on in the existing workflow.
    """
    lines = ["Answers to your clarifying questions:"]
    for index, question in enumerate(questions, start=1):
        answer = answers.get(question.id)
        if isinstance(answer, list):
            rendered = ", ".join(str(a) for a in answer) if answer else "(no preference)"
        elif answer is None or str(answer).strip() == "":
            rendered = "(no preference)"
        else:
            rendered = str(answer)
        lines.append(f"{index}. {question.text} → {rendered}")

    lines.append("")
    lines.append(
        "If you still need more information, emit another clarify-json block. "
        'Otherwise set "ready":true OR produce the final output without a '
        "clarify-json block."
    )
    return "\n".join(lines)

"""Operation prompts shared by both harness adapters.

These are harness-neutral: the same text is handed to Claude Code and to Codex.
Anything backend-specific (flags, sandbox modes, event shapes) lives in the
adapter modules, not here.
"""

from __future__ import annotations

from pathlib import Path

PLAN_PROMPT = """\
Read the software requirements specification at `{srs_path}` and produce a \
step-by-step implementation plan for this repository.

Ground the plan in the actual code: read the files you would change, and name \
them explicitly. Cover ordering, the files each step touches, edge cases, and \
how the work will be verified.

Rules:
- This is a planning task only. Do NOT modify, create, or delete any file.
- Do not run commands that change state.
- Your final message must BE the plan, in Markdown, starting with a top-level \
heading. No preamble, no "here is the plan", no questions.
"""


IMPLEMENT_PROMPT = """\
Implement the plan at `{plan_path}` in this repository.

The specification it derives from is at `{srs_path}` — consult it when the plan \
is ambiguous.

Rules:
- Work only inside this worktree.
- Follow the plan. If you find the plan is wrong, say so in your final message \
and implement the correct thing rather than silently diverging.
- Match the surrounding code's conventions, naming and comment density.
- Run the project's tests if it has them, and make them pass.
- Do not commit; leave the changes in the working tree.
- Your final message must summarise what you changed and anything you could not \
complete.
"""


REVIEW_PROMPT = """\
Review the uncommitted changes in this repository against the specification and \
plan they were built from.

- Specification: `{srs_path}`
- Plan: `{plan_path}`

Look for correctness bugs, security problems, missed requirements, and \
divergence from the plan. Prefer concrete, actionable findings over style notes.

Rules:
- This is a review task only. Do NOT modify, create, or delete any file.
- Report every issue you find, including low-confidence ones — a separate triage \
step decides what to act on. Do not filter by severity yourself.

Your final message must be a Markdown review document. End it with a fenced \
block listing the findings in machine-readable form:

```review-findings
{{
  "findings": [
    {{
      "id": "f1",
      "title": "Short summary of the issue",
      "severity": "high",
      "confidence": "high",
      "file": "path/to/file.py",
      "line": 42,
      "detail": "What is wrong and why it matters."
    }}
  ]
}}
```

`severity` is one of "high", "medium", "low". `confidence` is one of "high", \
"medium", "low". Use an empty `findings` array if you found nothing.
"""


FOLLOW_UP_PROMPT = """\
Address this specific review finding in the repository.

{finding}

Rules:
- Change only what is needed to resolve this finding.
- Do not commit; leave the changes in the working tree.
- Your final message must summarise the fix.
"""


def plan_prompt(srs_path: Path | str) -> str:
    return PLAN_PROMPT.format(srs_path=srs_path)


def implement_prompt(plan_path: Path | str, srs_path: Path | str) -> str:
    return IMPLEMENT_PROMPT.format(plan_path=plan_path, srs_path=srs_path)


def review_prompt(srs_path: Path | str, plan_path: Path | str) -> str:
    return REVIEW_PROMPT.format(srs_path=srs_path, plan_path=plan_path)


def follow_up_prompt(finding: str) -> str:
    return FOLLOW_UP_PROMPT.format(finding=finding)

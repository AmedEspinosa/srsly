"""The clarify-json protocol, ported from copilot_questions.lua.

Fixtures mirror what the Lua parser accepts so a model response that worked in
the Neovim workflow parses identically here.
"""

from __future__ import annotations

from workflow_orchestrator.services.clarify import (
    Question,
    format_answers,
    parse_payload,
    parse_questions_legacy,
    parse_response,
    strip_clarify_block,
)

STRUCTURED = """\
Here are my questions.

```clarify-json
{
  "v": 1,
  "ready": false,
  "questions": [
    {
      "id": "q1",
      "text": "Which auth provider?",
      "type": "single_select",
      "options": ["Auth0 (managed)", "Cognito (AWS-native)"],
      "allow_custom": false,
      "placeholder": ""
    },
    {
      "id": "q2",
      "text": "Any migration constraints?",
      "type": "text",
      "options": [],
      "allow_custom": true,
      "placeholder": "e.g. zero downtime"
    }
  ]
}
```
"""

LEGACY = """\
## Clarifying questions

1. Which auth provider should we use?
   - Auth0
   - Cognito
2. What is the rollout window?

---

Anything after the rule is ignored.
"""


def test_parses_structured_block() -> None:
    payload = parse_payload(STRUCTURED)
    assert payload is not None
    assert payload.ready is False
    assert payload.version == 1
    assert [q.id for q in payload.questions] == ["q1", "q2"]

    first = payload.questions[0]
    assert first.type == "single_select"
    assert first.options == ["Auth0 (managed)", "Cognito (AWS-native)"]
    assert first.allow_custom is False

    second = payload.questions[1]
    assert second.type == "text"
    assert second.allow_custom is True
    assert second.placeholder == "e.g. zero downtime"


def test_ready_true_with_no_questions() -> None:
    payload = parse_payload('```clarify-json\n{"v":1,"ready":true,"questions":[]}\n```')
    assert payload is not None
    assert payload.ready is True
    assert payload.questions == []


def test_missing_fields_get_lua_defaults() -> None:
    """The Lua defaults id to q<N>, type to text, allow_custom to true."""
    payload = parse_payload(
        '```clarify-json\n{"v":1,"questions":[{"text":"Bare question?"}]}\n```'
    )
    assert payload is not None
    question = payload.questions[0]
    assert question.id == "q1"
    assert question.type == "text"
    assert question.options == []
    assert question.allow_custom is True
    assert question.placeholder == ""
    assert payload.ready is False


def test_unknown_type_falls_back_to_text() -> None:
    payload = parse_payload(
        '```clarify-json\n{"v":1,"questions":[{"text":"?","type":"slider"}]}\n```'
    )
    assert payload is not None
    assert payload.questions[0].type == "text"


def test_malformed_json_returns_none() -> None:
    assert parse_payload("```clarify-json\n{not json}\n```") is None


def test_missing_questions_key_returns_none() -> None:
    assert parse_payload('```clarify-json\n{"v":1,"ready":true}\n```') is None


def test_no_fence_returns_none() -> None:
    assert parse_payload("Just prose, no block here.") is None


def test_legacy_markdown_fallback() -> None:
    payload = parse_questions_legacy(LEGACY)
    assert payload is not None
    assert payload.version == 0
    assert len(payload.questions) == 2
    assert payload.questions[0].text == "Which auth provider should we use?"
    assert payload.questions[0].options == ["Auth0", "Cognito"]
    assert payload.questions[0].type == "single_select"
    # The horizontal rule terminates the block.
    assert payload.questions[1].text == "What is the rollout window?"
    assert payload.questions[1].type == "text"


def test_parse_response_prefers_structured() -> None:
    combined = STRUCTURED + "\n" + LEGACY
    payload = parse_response(combined)
    assert payload is not None
    assert payload.version == 1  # structured won


def test_parse_response_falls_back_to_legacy() -> None:
    payload = parse_response(LEGACY)
    assert payload is not None
    assert payload.version == 0


def test_strip_clarify_block_leaves_prose() -> None:
    stripped = strip_clarify_block(STRUCTURED)
    assert "clarify-json" not in stripped
    assert stripped.startswith("Here are my questions.")


def test_format_answers_matches_lua_shape() -> None:
    questions = [
        Question(id="q1", text="Which provider?"),
        Question(id="q2", text="Constraints?"),
        Question(id="q3", text="Regions?", type="multi_select"),
    ]
    rendered = format_answers(
        questions, {"q1": "Auth0", "q2": "", "q3": ["us-east-1", "eu-west-1"]}
    )
    assert "1. Which provider? → Auth0" in rendered
    assert "2. Constraints? → (no preference)" in rendered
    assert "3. Regions? → us-east-1, eu-west-1" in rendered
    assert 'set "ready":true' in rendered


def test_format_answers_handles_empty_list() -> None:
    rendered = format_answers([Question(id="q1", text="Regions?")], {"q1": []})
    assert "(no preference)" in rendered

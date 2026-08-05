"""Harness adapters — FR-15, FR-16, §4.4, AC-9.

The JSONL fixtures below are verbatim lines captured from the installed CLIs
(`claude` 2.1.220 and `codex`), so a change in either event schema fails here
rather than at runtime.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from workflow_orchestrator.config import Settings
from workflow_orchestrator.harness import get_adapter
from workflow_orchestrator.harness.base import HarnessOperation, RunEvent
from workflow_orchestrator.harness.claude_code import ClaudeCodeAdapter
from workflow_orchestrator.harness.codex import CodexAdapter
from workflow_orchestrator.harness.pricing import estimate_cost, price_for
from workflow_orchestrator.models import Harness

# --- captured fixtures --------------------------------------------------------

CLAUDE_INIT = json.dumps(
    {"type": "system", "subtype": "init", "model": "claude-opus-5[1m]", "cwd": "/tmp/x"}
)
CLAUDE_TEXT_DELTA = json.dumps(
    {
        "type": "stream_event",
        "event": {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "PONG"},
        },
    }
)
CLAUDE_TOOL_USE = json.dumps(
    {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file": "a"}}
            ]
        },
    }
)
CLAUDE_RESULT = json.dumps(
    {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "# Plan\n\nStep one.",
        "total_cost_usd": 0.063638,
        "usage": {"input_tokens": 2, "output_tokens": 5},
    }
)
CLAUDE_ERROR_RESULT = json.dumps(
    {"type": "result", "subtype": "error", "is_error": True, "result": "boom"}
)

CODEX_THREAD = json.dumps({"type": "thread.started", "thread_id": "019fb917"})
CODEX_AGENT_MESSAGE = json.dumps(
    {
        "type": "item.completed",
        "item": {"id": "item_0", "type": "agent_message", "text": "# Plan\n\nStep one."},
    }
)
CODEX_COMMAND = json.dumps(
    {
        "type": "item.completed",
        "item": {"id": "item_1", "type": "command_execution", "command": "ls -la"},
    }
)
CODEX_TURN_COMPLETED = json.dumps(
    {
        "type": "turn.completed",
        "usage": {
            "input_tokens": 15504,
            "cached_input_tokens": 8960,
            "output_tokens": 6,
            "reasoning_output_tokens": 0,
        },
    }
)


@pytest.fixture
def adapters(settings: Settings) -> dict[str, object]:
    return {
        "claude_code": ClaudeCodeAdapter(settings),
        "codex": CodexAdapter(settings),
    }


# --- command construction -----------------------------------------------------


def test_claude_plan_is_read_only(settings: Settings, tmp_path: Path) -> None:
    """FR-16 — plan mode makes source edits impossible, not merely discouraged."""
    spec = ClaudeCodeAdapter(settings).command(
        HarnessOperation.PLAN, worktree=tmp_path, prompt="do the thing"
    )
    argv = list(spec.argv)
    assert argv[0] == "claude"
    assert "-p" in argv
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert argv[argv.index("--permission-mode") + 1] == "plan"
    assert spec.read_only is True
    assert spec.cwd == tmp_path

    assert argv[argv.index("--allowedTools") + 1] == "Read,Grep,Glob"

    # Regression: --allowedTools and --add-dir are variadic and greedily consume
    # every following non-option token. A prompt passed in argv gets swallowed
    # and the CLI exits 1 with "Input must be provided either through stdin or
    # as a prompt argument", so the prompt must travel on stdin.
    assert spec.stdin == "do the thing"
    assert "do the thing" not in argv


def test_claude_implement_allows_edits(settings: Settings, tmp_path: Path) -> None:
    spec = ClaudeCodeAdapter(settings).command(
        HarnessOperation.IMPLEMENT, worktree=tmp_path, prompt="build it"
    )
    argv = list(spec.argv)
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    assert spec.read_only is False


def test_codex_plan_uses_read_only_sandbox(settings: Settings, tmp_path: Path) -> None:
    """FR-16 — the Codex sandbox refuses writes outright."""
    out = tmp_path / "last.txt"
    spec = CodexAdapter(settings).command(
        HarnessOperation.PLAN, worktree=tmp_path, prompt="plan it", output_file=out
    )
    argv = list(spec.argv)
    assert argv[:3] == ["codex", "exec", "--json"]
    assert argv[argv.index("-s") + 1] == "read-only"
    assert argv[argv.index("--cd") + 1] == str(tmp_path)
    assert argv[argv.index("--output-last-message") + 1] == str(out)
    assert spec.read_only is True
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv
    # `-` makes codex exec read the prompt from stdin.
    assert argv[-1] == "-"
    assert spec.stdin == "plan it"
    assert "plan it" not in argv


def test_codex_implement_uses_workspace_write(settings: Settings, tmp_path: Path) -> None:
    spec = CodexAdapter(settings).command(
        HarnessOperation.IMPLEMENT, worktree=tmp_path, prompt="build it"
    )
    argv = list(spec.argv)
    assert argv[argv.index("-s") + 1] == "workspace-write"
    assert spec.read_only is False


def test_review_is_read_only_on_both_backends(
    adapters: dict[str, object], tmp_path: Path
) -> None:
    for adapter in adapters.values():
        spec = adapter.command(  # type: ignore[attr-defined]
            HarnessOperation.REVIEW, worktree=tmp_path, prompt="review it"
        )
        assert spec.read_only is True, adapter.name  # type: ignore[attr-defined]


# --- event translation --------------------------------------------------------


def test_claude_translates_text_deltas(adapters: dict[str, object]) -> None:
    event = adapters["claude_code"].parse_line(CLAUDE_TEXT_DELTA)  # type: ignore[attr-defined]
    assert event == RunEvent(event_type="text_delta", text="PONG", timestamp=event.timestamp)


def test_claude_translates_tool_use(adapters: dict[str, object]) -> None:
    event = adapters["claude_code"].parse_line(CLAUDE_TOOL_USE)  # type: ignore[attr-defined]
    assert event.event_type == "tool_use"
    assert event.text == "[Read]"


def test_claude_result_carries_authoritative_cost(adapters: dict[str, object]) -> None:
    """FR-21 — Claude reports total_cost_usd, so no estimation is needed."""
    event = adapters["claude_code"].parse_line(CLAUDE_RESULT)  # type: ignore[attr-defined]
    assert event.event_type == "result"
    assert event.cost_usd == pytest.approx(0.063638)
    assert event.token_count == 7
    assert event.text.startswith("# Plan")


def test_claude_error_result_is_flagged(adapters: dict[str, object]) -> None:
    event = adapters["claude_code"].parse_line(CLAUDE_ERROR_RESULT)  # type: ignore[attr-defined]
    assert event.event_type == "error"


def test_codex_translates_agent_message(adapters: dict[str, object]) -> None:
    event = adapters["codex"].parse_line(CODEX_AGENT_MESSAGE)  # type: ignore[attr-defined]
    assert event.event_type == "agent_message"
    assert event.text.startswith("# Plan")


def test_codex_translates_command_execution(adapters: dict[str, object]) -> None:
    event = adapters["codex"].parse_line(CODEX_COMMAND)  # type: ignore[attr-defined]
    assert event.event_type == "tool_use"
    assert event.text == "$ ls -la"


def test_codex_estimates_cost_from_tokens(adapters: dict[str, object]) -> None:
    """FR-21 — Codex reports no cost, so the ceiling needs an estimate."""
    event = adapters["codex"].parse_line(CODEX_TURN_COMPLETED)  # type: ignore[attr-defined]
    assert event.event_type == "result"
    assert event.token_count == 15510
    assert event.cost_usd is not None and event.cost_usd > 0


@pytest.mark.parametrize(
    "line", ["", "   ", "not json", "plain log line", '{"type":"unknown"}', "[]"]
)
def test_both_adapters_ignore_noise(adapters: dict[str, object], line: str) -> None:
    for adapter in adapters.values():
        assert adapter.parse_line(line) is None  # type: ignore[attr-defined]


def test_codex_tolerates_stderr_preamble(adapters: dict[str, object]) -> None:
    """The CLI prints non-JSON warnings before the stream; they must not crash."""
    noise = "2026-07-31T16:52:57Z ERROR codex_models_manager::cache: failed to load"
    assert adapters["codex"].parse_line(noise) is None  # type: ignore[attr-defined]
    assert adapters["codex"].parse_line(CODEX_THREAD).event_type == "system"  # type: ignore[attr-defined]


# --- final text extraction ----------------------------------------------------


def test_claude_final_text_prefers_result(adapters: dict[str, object]) -> None:
    adapter = adapters["claude_code"]
    events = [
        adapter.parse_line(CLAUDE_TEXT_DELTA),  # type: ignore[attr-defined]
        adapter.parse_line(CLAUDE_RESULT),  # type: ignore[attr-defined]
    ]
    assert adapter.final_text(events).startswith("# Plan")  # type: ignore[attr-defined]


def test_claude_final_text_falls_back_to_deltas(adapters: dict[str, object]) -> None:
    adapter = adapters["claude_code"]
    events = [RunEvent(event_type="text_delta", text="# Plan\n"), RunEvent("text_delta", "step")]
    assert adapter.final_text(events) == "# Plan\nstep"  # type: ignore[attr-defined]


def test_codex_final_text_uses_agent_message(adapters: dict[str, object]) -> None:
    adapter = adapters["codex"]
    events = [adapter.parse_line(CODEX_AGENT_MESSAGE)]  # type: ignore[attr-defined]
    assert adapter.final_text(events).startswith("# Plan")  # type: ignore[attr-defined]


# --- pricing ------------------------------------------------------------------


def test_unknown_model_falls_back_to_conservative_price() -> None:
    assert price_for("something-unreleased").output_per_mtok >= 25.00
    assert price_for(None).output_per_mtok >= 25.00


def test_dated_snapshot_resolves_to_base_model() -> None:
    assert price_for("gpt-5-2026-01-01").input_per_mtok == price_for("gpt-5").input_per_mtok


def test_cached_tokens_are_billed_at_a_discount() -> None:
    full = estimate_cost("gpt-5", input_tokens=1_000_000, output_tokens=0)
    cached = estimate_cost(
        "gpt-5", input_tokens=1_000_000, output_tokens=0, cached_input_tokens=1_000_000
    )
    assert cached < full


def test_price_override_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKFLOW_PRICE_GPT_5_INPUT", "99")
    monkeypatch.setenv("WORKFLOW_PRICE_GPT_5_OUTPUT", "199")
    assert price_for("gpt-5").input_per_mtok == 99.0


# --- registry / isolation -----------------------------------------------------


def test_registry_returns_the_right_adapter(settings: Settings) -> None:
    assert get_adapter(Harness.CLAUDE_CODE, settings).name == "claude_code"
    assert get_adapter(Harness.CODEX, settings).name == "codex"
    assert get_adapter("codex", settings).name == "codex"


def test_opposite_harness_pairs_correctly() -> None:
    """The enum helper returns the alternate harness member."""
    assert Harness.CLAUDE_CODE.other() is Harness.CODEX
    assert Harness.CODEX.other() is Harness.CLAUDE_CODE

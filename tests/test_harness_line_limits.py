"""Regression: harness JSONL lines routinely exceed asyncio's readline limit.

``asyncio.create_subprocess_exec`` builds its StreamReader with a 64 KiB
``limit``, and iterating the stream calls ``readline()``. Claude Code runs as
``-p --output-format stream-json --verbose``, which echoes whole tool payloads
inline — a single ``Read`` of a moderately large source file becomes one JSON
line far past that limit. The first plan run against a real repository died
with::

    ValueError: Separator is found, but chunk is longer than limit

and, because that is not a ``HarnessError``, ``/plan/run`` returned an
unhandled 500 rather than its structured 502.

Earlier live verification passed only because it ran in a scratch repo whose
files never produced a long line.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
import structlog

from workflow_orchestrator.config import Settings
from workflow_orchestrator.harness.base import HarnessOperation
from workflow_orchestrator.harness.claude_code import ClaudeCodeAdapter
from workflow_orchestrator.harness.lines import MAX_LINE_BYTES, LineBuffer
from workflow_orchestrator.harness.runner import HarnessError, collect

#: Comfortably past asyncio's 64 KiB default, the size that actually broke.
BIG_TEXT = "x" * (1024 * 1024)


def make_stub(path: Path, body: str) -> Path:
    path.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _spec(settings: Settings, worktree: Path):
    return ClaudeCodeAdapter(settings).command(
        HarnessOperation.PLAN, worktree=worktree, prompt="plan it", output_file=None
    )


# --- LineBuffer ------------------------------------------------------------


def test_line_buffer_reassembles_across_chunk_boundaries() -> None:
    buffer = LineBuffer()
    assert list(buffer.feed(b'{"a":')) == []
    assert list(buffer.feed(b'1}\n{"b":2}\n')) == ['{"a":1}', '{"b":2}']
    assert buffer.flush() is None


def test_line_buffer_emits_a_final_line_without_a_newline() -> None:
    """A CLI that omits the trailing newline must not lose its last event."""
    buffer = LineBuffer()
    assert list(buffer.feed(b'{"a":1}\n{"b":2}')) == ['{"a":1}']
    assert buffer.flush() == '{"b":2}'
    assert buffer.flush() is None


def test_line_buffer_handles_a_line_far_past_the_asyncio_limit() -> None:
    payload = json.dumps({"type": "assistant", "text": BIG_TEXT}).encode()
    buffer = LineBuffer()
    lines = list(buffer.feed(payload + b"\n"))
    assert len(lines) == 1
    assert json.loads(lines[0])["text"] == BIG_TEXT


def test_oversized_line_is_dropped_not_raised() -> None:
    """A pathological line must not abort a multi-minute run.

    ``parse_line`` already returns None for anything unparseable, so losing one
    telemetry line degrades gracefully; killing the run does not.
    """
    buffer = LineBuffer(max_line_bytes=1024)
    with structlog.testing.capture_logs() as logs:
        assert list(buffer.feed(b"y" * 5000)) == []
        # The run continues: the next well-formed line still arrives.
        assert list(buffer.feed(b"\n" + b'{"ok":1}' + b"\n")) == ['{"ok":1}']
    assert any(entry["event"] == "harness.line_too_long" for entry in logs)


def test_default_cap_is_generous_enough_for_real_payloads() -> None:
    assert MAX_LINE_BYTES > 16 * 1024 * 1024


# --- stream_command --------------------------------------------------------


@pytest.fixture
def big_line_stub(tmp_path: Path) -> Path:
    """A harness whose middle event is ~1 MB on one line — the reported case.

    The stub builds the payload itself so the generated source stays small.
    """
    body = f"""
import sys, json
sys.stdin.read()
BIG = 'x' * {len(BIG_TEXT)}
print(json.dumps({{"type": "system", "subtype": "init", "model": "stub"}}))
print(json.dumps({{"type": "stream_event",
                  "event": {{"type": "content_block_delta",
                            "delta": {{"type": "text_delta", "text": BIG}}}}}}))
print(json.dumps({{"type": "result", "subtype": "success", "is_error": False,
                  "result": "# Plan", "total_cost_usd": 0.01,
                  "usage": {{"input_tokens": 10, "output_tokens": 20}}}}))
"""
    return make_stub(tmp_path / "claude-bigline", body)


async def test_million_character_line_does_not_kill_the_run(
    settings: Settings, big_line_stub: Path, repo: Path
) -> None:
    """This is the exact failure from `POST /plan/run` on trucking-backend."""
    settings.WORKFLOW_CLAUDE_BIN = str(big_line_stub)
    adapter = ClaudeCodeAdapter(settings)

    events = await collect(_spec(settings, repo), adapter)

    texts = [e.text for e in events if e.text]
    assert any(len(t) >= len(BIG_TEXT) for t in texts), "long line was lost"
    # And the stream kept going: the result event still arrived.
    assert any(e.event_type == "result" for e in events)


@pytest.fixture
def big_stderr_stub(tmp_path: Path) -> Path:
    """Fails after writing a single enormous stderr line."""
    body = (
        "import sys\n"
        "sys.stdin.read()\n"
        "sys.stderr.write('e' * 300000 + ' boom\\n')\n"
        "sys.exit(3)\n"
    )
    return make_stub(tmp_path / "claude-bigstderr", body)


async def test_oversized_stderr_still_produces_a_harness_error(
    settings: Settings, big_stderr_stub: Path, repo: Path
) -> None:
    """stderr had the same readline bug, hidden inside a never-awaited task."""
    settings.WORKFLOW_CLAUDE_BIN = str(big_stderr_stub)
    adapter = ClaudeCodeAdapter(settings)

    with pytest.raises(HarnessError) as excinfo:
        await collect(_spec(settings, repo), adapter)

    message = str(excinfo.value)
    assert "exited 3" in message
    assert "boom" in message, "the tail of stderr must survive truncation"


@pytest.fixture
def no_trailing_newline_stub(tmp_path: Path) -> Path:
    body = (
        "import sys, json\n"
        "sys.stdin.read()\n"
        "sys.stdout.write(json.dumps({'type': 'result', 'subtype': 'success',\n"
        "  'is_error': False, 'result': '# Plan', 'total_cost_usd': 0.5,\n"
        "  'usage': {'input_tokens': 1, 'output_tokens': 2}}))\n"
    )
    return make_stub(tmp_path / "claude-nonewline", body)


async def test_final_line_without_a_newline_is_not_dropped(
    settings: Settings, no_trailing_newline_stub: Path, repo: Path
) -> None:
    settings.WORKFLOW_CLAUDE_BIN = str(no_trailing_newline_stub)
    adapter = ClaudeCodeAdapter(settings)

    events = await collect(_spec(settings, repo), adapter)
    results = [e for e in events if e.event_type == "result"]
    assert len(results) == 1
    assert results[0].cost_usd == 0.5

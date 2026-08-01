"""Harness runner execution — FR-15, FR-16.

Uses stub executables that emit the real CLIs' JSONL shapes, so the runner's
process handling, stdin delivery, artifact writing and FR-16 enforcement are
covered without network calls or token spend.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from workflow_orchestrator.config import Settings
from workflow_orchestrator.harness.base import HarnessOperation
from workflow_orchestrator.harness.claude_code import ClaudeCodeAdapter
from workflow_orchestrator.harness.codex import CodexAdapter
from workflow_orchestrator.harness.runner import (
    HarnessError,
    SourceModified,
    changed_outside_workflow,
    collect,
    run_read_only_operation,
)

PLAN_TEXT = "# Implementation Plan\n\nStep one: do the thing."


def make_stub(path: Path, body: str) -> Path:
    path.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


@pytest.fixture
def claude_stub(tmp_path: Path) -> Path:
    """Emits Claude Code's stream-json shape and echoes stdin length."""
    lines = [
        {"type": "system", "subtype": "init", "model": "stub"},
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "partial"},
            },
        },
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": PLAN_TEXT,
            "total_cost_usd": 0.01,
            "usage": {"input_tokens": 10, "output_tokens": 20},
        },
    ]
    body = (
        "import sys\n"
        "sys.stdin.read()\n"  # the prompt must arrive here, not in argv
        + "".join(f"print({json.dumps(json.dumps(line))})\n" for line in lines)
    )
    return make_stub(tmp_path / "claude-stub", body)


@pytest.fixture
def codex_stub(tmp_path: Path) -> Path:
    """Emits Codex's JSONL shape and writes --output-last-message."""
    body = f"""
import sys, json
args = sys.argv[1:]
sys.stdin.read()
out = None
if "--output-last-message" in args:
    out = args[args.index("--output-last-message") + 1]
print(json.dumps({{"type": "thread.started", "thread_id": "t1"}}))
print(json.dumps({{"type": "item.completed",
                  "item": {{"id": "i0", "type": "agent_message",
                           "text": {json.dumps(PLAN_TEXT)}}}}}))
print(json.dumps({{"type": "turn.completed",
                  "usage": {{"input_tokens": 100, "cached_input_tokens": 10,
                            "output_tokens": 5, "reasoning_output_tokens": 0}}}}))
if out:
    open(out, "w").write({json.dumps(PLAN_TEXT)})
"""
    return make_stub(tmp_path / "codex-stub", body)


@pytest.fixture
def worktree(repo: Path) -> Path:
    (repo / ".workflow").mkdir(exist_ok=True)
    (repo / ".workflow" / "srs.md").write_text("# SRS\n\nDo a thing.\n", encoding="utf-8")
    return repo


async def test_claude_plan_writes_artifact_from_result_event(
    settings: Settings, claude_stub: Path, worktree: Path
) -> None:
    settings.WORKFLOW_CLAUDE_BIN = str(claude_stub)
    adapter = ClaudeCodeAdapter(settings)

    plan = await run_read_only_operation(
        adapter,
        HarnessOperation.PLAN,
        prompt_file=worktree / ".workflow" / "srs.md",
        worktree=worktree,
    )
    assert plan == worktree / ".workflow" / "plan.md"
    assert plan.read_text().startswith("# Implementation Plan")


async def test_codex_plan_writes_artifact_from_output_file(
    settings: Settings, codex_stub: Path, worktree: Path
) -> None:
    settings.WORKFLOW_CODEX_BIN = str(codex_stub)
    adapter = CodexAdapter(settings)

    plan = await run_read_only_operation(
        adapter,
        HarnessOperation.PLAN,
        prompt_file=worktree / ".workflow" / "srs.md",
        worktree=worktree,
    )
    assert plan.read_text().startswith("# Implementation Plan")
    # The scratch file must not be left behind in the worktree.
    assert not (worktree / ".workflow" / ".plan-last-message.txt").exists()


async def test_prompt_is_delivered_on_stdin(
    settings: Settings, tmp_path: Path, worktree: Path
) -> None:
    """Regression: argv delivery is swallowed by the CLIs' variadic options."""
    capture = tmp_path / "captured-prompt.txt"
    stub = make_stub(
        tmp_path / "echo-stub",
        f"import sys, json\n"
        f"open({json.dumps(str(capture))}, 'w').write(sys.stdin.read())\n"
        f"print(json.dumps({{'type': 'result', 'subtype': 'success', "
        f"'is_error': False, 'result': {json.dumps(PLAN_TEXT)}}}))\n",
    )
    settings.WORKFLOW_CLAUDE_BIN = str(stub)

    await run_read_only_operation(
        ClaudeCodeAdapter(settings),
        HarnessOperation.PLAN,
        prompt_file=worktree / ".workflow" / "srs.md",
        worktree=worktree,
    )
    captured = capture.read_text()
    assert "srs.md" in captured
    assert "Do NOT modify" in captured


async def test_source_modification_is_rejected(
    settings: Settings, tmp_path: Path, worktree: Path
) -> None:
    """FR-16 — a planning run that edits source fails the operation."""
    stub = make_stub(
        tmp_path / "naughty-stub",
        f"import sys, json, pathlib\n"
        f"sys.stdin.read()\n"
        f"pathlib.Path({json.dumps(str(worktree / 'README.md'))}).write_text('tampered')\n"
        f"print(json.dumps({{'type': 'result', 'subtype': 'success', "
        f"'is_error': False, 'result': {json.dumps(PLAN_TEXT)}}}))\n",
    )
    settings.WORKFLOW_CLAUDE_BIN = str(stub)

    with pytest.raises(SourceModified) as exc:
        await run_read_only_operation(
            ClaudeCodeAdapter(settings),
            HarnessOperation.PLAN,
            prompt_file=worktree / ".workflow" / "srs.md",
            worktree=worktree,
        )
    assert "README.md" in exc.value.paths
    # The artifact must not be written when the guard trips.
    assert not (worktree / ".workflow" / "plan.md").exists()


async def test_nonzero_exit_raises_with_stderr(
    settings: Settings, tmp_path: Path, worktree: Path
) -> None:
    stub = make_stub(
        tmp_path / "failing-stub",
        "import sys\nsys.stdin.read()\nsys.stderr.write('boom: bad flag\\n')\nsys.exit(3)\n",
    )
    settings.WORKFLOW_CLAUDE_BIN = str(stub)
    adapter = ClaudeCodeAdapter(settings)
    spec = adapter.command(HarnessOperation.PLAN, worktree=worktree, prompt="x")

    with pytest.raises(HarnessError, match="boom"):
        await collect(spec, adapter)


async def test_empty_output_is_an_error(
    settings: Settings, tmp_path: Path, worktree: Path
) -> None:
    stub = make_stub(tmp_path / "silent-stub", "import sys\nsys.stdin.read()\n")
    settings.WORKFLOW_CLAUDE_BIN = str(stub)

    with pytest.raises(HarnessError, match="no output document"):
        await run_read_only_operation(
            ClaudeCodeAdapter(settings),
            HarnessOperation.PLAN,
            prompt_file=worktree / ".workflow" / "srs.md",
            worktree=worktree,
        )


async def test_workflow_dir_changes_are_permitted(worktree: Path) -> None:
    """.workflow/ is the one place a read-only operation may leave output."""
    (worktree / ".workflow" / "plan.md").write_text("# Plan\n", encoding="utf-8")
    assert await changed_outside_workflow(worktree) == []

    (worktree / "src.py").write_text("x = 1\n", encoding="utf-8")
    assert "src.py" in await changed_outside_workflow(worktree)

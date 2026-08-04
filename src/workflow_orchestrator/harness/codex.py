"""Codex adapter.

Invocation: ``codex exec --json --cd <worktree> -s <sandbox>``.

The stream is JSONL. Shapes below are taken from the installed CLI:

    {"type":"thread.started","thread_id":"..."}
    {"type":"turn.started"}
    {"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"..."}}
    {"type":"turn.completed","usage":{"input_tokens":15504,"cached_input_tokens":8960,
                                      "output_tokens":6,"reasoning_output_tokens":0}}

Codex reports **token counts but no cost**, so the FR-21 ceiling is enforced
against an estimate from :mod:`workflow_orchestrator.harness.pricing`.

``-s read-only`` makes FR-16 structural for the plan and review operations: the
sandbox refuses writes, so the harness cannot touch source even if the prompt is
ignored. The final message is captured with ``--output-last-message`` and the
orchestrator — not the harness — writes the artifact.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

from ..config import Settings
from ..logging import get_logger
from .base import CommandSpec, HarnessOperation, RunEvent
from .pricing import estimate_cost

log = get_logger(__name__)

NAME = "codex"

SANDBOX_READ_ONLY = "read-only"
SANDBOX_WORKSPACE_WRITE = "workspace-write"


class CodexAdapter:
    name = NAME

    #: Host environment variables the sandbox needs to authenticate. Names only;
    #: values are forwarded by the runner and never read here (NFR-2).
    credential_env = ("OPENAI_API_KEY", "CODEX_API_KEY")

    #: `turn.completed` usage covers that turn only, so a multi-turn run
    #: reports increments that must be summed.
    cost_is_cumulative = False

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._model: str | None = None

    # --- command construction ------------------------------------------------

    def command(
        self,
        operation: HarnessOperation,
        *,
        worktree: Path,
        prompt: str,
        output_file: Path | None = None,
    ) -> CommandSpec:
        read_only = operation.read_only
        sandbox = SANDBOX_READ_ONLY if read_only else SANDBOX_WORKSPACE_WRITE

        argv: list[str] = [
            self._settings.WORKFLOW_CODEX_BIN,
            "exec",
            "--json",
            "--cd",
            str(worktree),
            "-s",
            sandbox,
        ]
        if output_file is not None:
            argv += ["--output-last-message", str(output_file)]
        if not read_only:
            # The worktree is already isolated; skip the interactive approval
            # prompts that would otherwise block a detached run.
            argv.append("--dangerously-bypass-approvals-and-sandbox")

        # `-` tells codex exec to read the prompt from stdin.
        argv.append("-")

        return CommandSpec(
            argv=tuple(argv),
            cwd=worktree,
            env={},
            stdin=prompt,
            output_file=output_file,
            read_only=read_only,
        )

    # --- event translation ---------------------------------------------------

    def parse_line(self, line: str) -> RunEvent | None:
        line = line.strip()
        if not line or not line.startswith("{"):
            return None
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None

        kind = payload.get("type")

        if kind == "thread.started":
            return RunEvent(
                event_type="system",
                text=f"thread started ({payload.get('thread_id', 'unknown')})",
            )

        if kind in ("item.started", "item.completed", "item.updated"):
            return self._parse_item(kind, payload.get("item") or {})

        if kind == "turn.completed":
            return self._parse_turn_completed(payload)

        if kind == "turn.failed" or kind == "error":
            message = payload.get("error") or payload.get("message") or "codex error"
            if isinstance(message, dict):
                message = message.get("message") or str(message)
            return RunEvent(event_type="error", text=str(message))

        return None

    def _parse_item(self, kind: str, item: dict[str, object]) -> RunEvent | None:
        if not isinstance(item, dict):
            return None
        item_type = str(item.get("type") or "item")

        # Only completed items carry final text; started/updated are progress.
        if kind != "item.completed":
            return RunEvent(event_type=item_type, text="")

        if item_type == "agent_message":
            return RunEvent(event_type="agent_message", text=str(item.get("text") or ""))
        if item_type == "reasoning":
            return RunEvent(event_type="reasoning", text=str(item.get("text") or ""))
        if item_type == "command_execution":
            command = str(item.get("command") or "")
            return RunEvent(event_type="tool_use", text=f"$ {command}")
        if item_type == "file_change":
            changes = item.get("changes")
            names = ""
            if isinstance(changes, list):
                names = ", ".join(
                    str(c.get("path")) for c in changes if isinstance(c, dict)
                )
            return RunEvent(event_type="file_change", text=names or "(files changed)")
        if item_type == "error":
            return RunEvent(event_type="error", text=str(item.get("message") or "error"))

        return RunEvent(event_type=item_type, text=str(item.get("text") or ""))

    def _parse_turn_completed(self, payload: dict[str, object]) -> RunEvent:
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            return RunEvent(event_type="result", text="")

        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        cached = int(usage.get("cached_input_tokens") or 0)
        reasoning = int(usage.get("reasoning_output_tokens") or 0)

        # Codex has no cost field; estimate it so the ceiling can be enforced.
        cost = estimate_cost(
            self._model,
            input_tokens=input_tokens,
            output_tokens=output_tokens + reasoning,
            cached_input_tokens=cached,
        )
        return RunEvent(
            event_type="result",
            text="",
            token_count=input_tokens + output_tokens + reasoning,
            cost_usd=cost,
        )

    # --- final-output extraction ---------------------------------------------

    def final_text(self, events: list[RunEvent]) -> str:
        for event in reversed(events):
            if event.event_type == "agent_message" and event.text.strip():
                return event.text
        return ""

    # --- SRS §4.4 operations -------------------------------------------------

    async def plan(self, prompt_file: Path, worktree: Path) -> Path:
        from .runner import run_read_only_operation

        return await run_read_only_operation(
            self, HarnessOperation.PLAN, prompt_file=prompt_file, worktree=worktree
        )

    async def implement(
        self, plan_file: Path, worktree: Path
    ) -> AsyncIterator[RunEvent]:
        from .runner import stream_operation

        async for event in stream_operation(
            self, HarnessOperation.IMPLEMENT, prompt_file=plan_file, worktree=worktree
        ):
            yield event

    async def review(
        self, target: Path, worktree: Path, context_files: list[Path]
    ) -> Path:
        from .runner import run_review_operation

        return await run_review_operation(
            self, target=target, worktree=worktree, context_files=context_files
        )

    async def as_built(
        self, worktree: Path, context_files: list[Path], merged_diff: Path | None
    ) -> Path:
        from .runner import run_as_built_operation

        return await run_as_built_operation(
            self,
            worktree=worktree,
            context_files=context_files,
            merged_diff=merged_diff,
        )

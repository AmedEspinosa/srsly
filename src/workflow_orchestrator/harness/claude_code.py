"""Claude Code adapter.

Invocation: ``claude -p --output-format stream-json --verbose``.

The stream is JSONL. Shapes below are taken from the installed CLI (2.1.220):

    {"type":"system","subtype":"init",...}
    {"type":"stream_event","event":{"type":"content_block_delta",
                                    "delta":{"type":"text_delta","text":"..."}}}
    {"type":"assistant","message":{"content":[{"type":"text","text":"..."},
                                              {"type":"tool_use","name":"Read",...}]}}
    {"type":"result","subtype":"success","result":"<final text>",
     "total_cost_usd":0.0636,"usage":{...},"is_error":false}

``result.total_cost_usd`` is authoritative for the FR-21 cost ceiling, so no
price estimation is needed on this backend.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

from ..config import Settings
from ..logging import get_logger
from .base import CommandSpec, HarnessOperation, RunEvent

log = get_logger(__name__)

NAME = "claude_code"

#: Read-only tool set for the plan and review operations. Combined with
#: ``--permission-mode plan`` this makes FR-16 structural rather than advisory:
#: the CLI is not permitted to edit anything.
READ_ONLY_TOOLS = ("Read", "Grep", "Glob")


class ClaudeCodeAdapter:
    name = NAME

    #: Host environment variables the sandbox needs to authenticate. Names only;
    #: values are forwarded by the runner and never read here (NFR-2).
    credential_env = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")

    #: `result.total_cost_usd` is the cumulative cost of the whole session,
    #: re-reported in full on every result event — it must not be summed.
    cost_is_cumulative = True

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    # --- command construction ------------------------------------------------

    def command(
        self,
        operation: HarnessOperation,
        *,
        worktree: Path,
        prompt: str,
        output_file: Path | None = None,
    ) -> CommandSpec:
        argv: list[str] = [
            self._settings.WORKFLOW_CLAUDE_BIN,
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--add-dir",
            str(worktree),
        ]

        read_only = operation.read_only
        if read_only:
            # Plan mode refuses edits outright; the allow-list narrows it further.
            #
            # `--allowedTools` is variadic (`<tools...>`), so passing the tools as
            # separate argv entries makes it swallow the trailing prompt and the
            # CLI dies with "Input must be provided either through stdin or as a
            # prompt argument". The comma-separated form takes exactly one value.
            argv += ["--permission-mode", "plan"]
            argv += ["--allowedTools", ",".join(READ_ONLY_TOOLS)]
        else:
            argv += ["--permission-mode", "acceptEdits"]

        # The prompt goes on stdin, never argv — see CommandSpec.stdin.
        return CommandSpec(
            argv=tuple(argv),
            cwd=worktree,
            env={},
            stdin=prompt,
            output_file=None,  # the final text arrives in the result event
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

        if kind == "stream_event":
            return self._parse_stream_event(payload.get("event") or {})

        if kind == "assistant":
            return self._parse_assistant(payload.get("message") or {})

        if kind == "result":
            return self._parse_result(payload)

        if kind == "system":
            subtype = payload.get("subtype")
            if subtype == "init":
                return RunEvent(
                    event_type="system",
                    text=f"session started (model={payload.get('model', 'unknown')})",
                )
            return None

        return None

    def _parse_stream_event(self, event: dict[str, object]) -> RunEvent | None:
        if event.get("type") != "content_block_delta":
            return None
        delta = event.get("delta")
        if not isinstance(delta, dict):
            return None
        if delta.get("type") == "text_delta":
            return RunEvent(event_type="text_delta", text=str(delta.get("text") or ""))
        if delta.get("type") == "thinking_delta":
            return RunEvent(
                event_type="thinking_delta", text=str(delta.get("thinking") or "")
            )
        return None

    def _parse_assistant(self, message: dict[str, object]) -> RunEvent | None:
        blocks = message.get("content")
        if not isinstance(blocks, list):
            return None
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                name = str(block.get("name") or "tool")
                return RunEvent(event_type="tool_use", text=f"[{name}]")
        return None

    def _parse_result(self, payload: dict[str, object]) -> RunEvent:
        usage = payload.get("usage")
        tokens: int | None = None
        if isinstance(usage, dict):
            tokens = int(usage.get("input_tokens") or 0) + int(
                usage.get("output_tokens") or 0
            )

        cost = payload.get("total_cost_usd")
        is_error = bool(payload.get("is_error"))

        return RunEvent(
            event_type="error" if is_error else "result",
            text=str(payload.get("result") or ""),
            token_count=tokens,
            cost_usd=float(cost) if isinstance(cost, int | float) else None,
        )

    # --- final-output extraction ---------------------------------------------

    def final_text(self, events: list[RunEvent]) -> str:
        """The document a read-only operation produced.

        Prefer the ``result`` event's text; fall back to concatenated text deltas
        if the CLI ever stops populating it.
        """
        for event in reversed(events):
            if event.event_type == "result" and event.text.strip():
                return event.text
        return "".join(e.text for e in events if e.event_type == "text_delta")

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

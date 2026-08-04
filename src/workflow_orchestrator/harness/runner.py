"""Shared execution for harness operations.

Backend-neutral: it drives whatever :class:`CommandSpec` an adapter builds and
translates lines through that adapter's ``parse_line``. Both adapters share this
code, which is what keeps AC-9's "no backend-specific logic outside the adapter
modules" true.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import suppress
from pathlib import Path

from ..logging import get_logger
from .base import CommandSpec, HarnessOperation, RunEvent
from .lines import LineBuffer
from .prompts import as_built_prompt, plan_prompt, review_prompt

log = get_logger(__name__)

WORKFLOW_DIR = ".workflow"
DEFAULT_TIMEOUT_SECONDS = 30 * 60

#: Bytes pulled from the pipe per read. Lines are reassembled by LineBuffer, so
#: this is a throughput knob, not a limit on line length.
READ_CHUNK_BYTES = 64 * 1024

#: StreamReader buffer size. Only ``readline``/``readuntil`` enforce it as a
#: hard limit and this module uses neither, but raising it above the 64 KiB
#: default keeps the transport from pausing and resuming on every long line.
STREAM_LIMIT_BYTES = 1024 * 1024

#: Only ``stderr.strip()[-800:]`` is ever reported, so there is no reason to
#: hold a failing process's entire error output in memory.
MAX_STDERR_BYTES = 64 * 1024


class HarnessError(RuntimeError):
    pass


class SourceModified(HarnessError):
    """FR-16 — a read-only operation touched something outside ``.workflow/``."""

    def __init__(self, paths: list[str]) -> None:
        self.paths = paths
        super().__init__(
            "read-only harness operation modified files outside .workflow/: "
            + ", ".join(paths)
        )


async def stream_command(
    spec: CommandSpec, adapter: object, *, timeout: float = DEFAULT_TIMEOUT_SECONDS
) -> AsyncIterator[RunEvent]:
    """Run ``spec`` and yield translated events as they arrive.

    Reads fixed-size chunks rather than iterating the stream. Iteration calls
    ``readline()``, which raises ``ValueError`` once a line exceeds the reader's
    ``limit`` — and these CLIs emit lines megabytes long, because the JSONL
    stream carries whole tool payloads inline. That killed the first plan run
    against a real repository.
    """
    env = {**os.environ, **spec.env}
    process = await asyncio.create_subprocess_exec(
        *spec.argv,
        cwd=str(spec.cwd),
        env=env,
        stdin=asyncio.subprocess.PIPE if spec.stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=STREAM_LIMIT_BYTES,
    )

    if spec.stdin is not None and process.stdin is not None:
        # Write the prompt and close stdin so the CLI stops waiting for more.
        process.stdin.write(spec.stdin.encode("utf-8"))
        await process.stdin.drain()
        process.stdin.close()

    assert process.stdout is not None
    stderr_tail = bytearray()

    async def drain_stderr() -> None:
        assert process.stderr is not None
        while chunk := await process.stderr.read(READ_CHUNK_BYTES):
            stderr_tail.extend(chunk)
            if len(stderr_tail) > MAX_STDERR_BYTES:
                del stderr_tail[:-MAX_STDERR_BYTES]

    stderr_task = asyncio.create_task(drain_stderr())
    buffer = LineBuffer()

    def _translate(line: str) -> RunEvent | None:
        return adapter.parse_line(line)  # type: ignore[attr-defined]

    try:
        async with asyncio.timeout(timeout):
            while chunk := await process.stdout.read(READ_CHUNK_BYTES):
                for line in buffer.feed(chunk):
                    event = _translate(line)
                    if event is not None:
                        yield event
            # A CLI that omits the final newline still has one event to give.
            trailing = buffer.flush()
            if trailing is not None:
                event = _translate(trailing)
                if event is not None:
                    yield event
            await process.wait()
    except TimeoutError:
        process.kill()
        await process.wait()
        raise
    except (ValueError, OSError) as exc:
        # Stream-level failures must reach the API as a harness error, not as an
        # unhandled 500 — /plan/run only catches HarnessError.
        process.kill()
        await process.wait()
        raise HarnessError(f"{spec.argv[0]} output could not be read: {exc}") from exc
    finally:
        stderr_task.cancel()
        with suppress(asyncio.CancelledError):
            await stderr_task

    if process.returncode != 0:
        stderr = bytes(stderr_tail).decode("utf-8", errors="replace")
        raise HarnessError(
            f"{spec.argv[0]} exited {process.returncode}: {stderr.strip()[-800:]}"
        )


async def collect(
    spec: CommandSpec, adapter: object, *, timeout: float = DEFAULT_TIMEOUT_SECONDS
) -> list[RunEvent]:
    return [event async for event in stream_command(spec, adapter, timeout=timeout)]


async def changed_outside_workflow(worktree: Path, git_bin: str = "git") -> list[str]:
    """Paths modified outside ``.workflow/`` — the FR-16 assertion."""
    from ..services.process import run_command

    result = await run_command([git_bin, "status", "--porcelain"], cwd=worktree)
    if not result.ok:
        return []

    offenders: list[str] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        entry = line[3:] if len(line) > 3 else line
        if " -> " in entry:
            entry = entry.split(" -> ", 1)[1]
        path = entry.strip().strip('"')
        if not path.startswith(f"{WORKFLOW_DIR}/"):
            offenders.append(path)
    return offenders


async def _write_artifact(
    spec: CommandSpec, adapter: object, events: list[RunEvent], target: Path
) -> Path:
    """Persist a read-only operation's output.

    The harness never writes the artifact itself — either the CLI dropped its
    final message into ``output_file`` (Codex) or the text arrived in the event
    stream (Claude Code). Writing it here is what lets both backends run with
    writes disabled entirely.
    """
    text = ""
    if spec.output_file is not None and spec.output_file.exists():
        text = spec.output_file.read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        text = adapter.final_text(events)  # type: ignore[attr-defined]
    if not text.strip():
        raise HarnessError("harness produced no output document")

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text.rstrip() + "\n", encoding="utf-8")
    return target


async def run_read_only_operation(
    adapter: object,
    operation: HarnessOperation,
    *,
    prompt_file: Path,
    worktree: Path,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> Path:
    """FR-15/FR-16 — plan without modifying source files."""
    target = worktree / WORKFLOW_DIR / f"{operation.value}.md"
    scratch = worktree / WORKFLOW_DIR / f".{operation.value}-last-message.txt"

    spec = adapter.command(  # type: ignore[attr-defined]
        operation,
        worktree=worktree,
        prompt=plan_prompt(prompt_file),
        output_file=scratch,
    )

    events = await collect(spec, adapter, timeout=timeout)

    offenders = await changed_outside_workflow(worktree)
    if offenders:
        raise SourceModified(offenders)

    result = await _write_artifact(spec, adapter, events, target)
    scratch.unlink(missing_ok=True)
    log.info(
        "harness.read_only_operation_complete",
        harness=getattr(adapter, "name", "unknown"),
        operation=operation.value,
        artifact=str(result),
        events=len(events),
    )
    return result


async def run_review_operation(
    adapter: object,
    *,
    target: Path,
    worktree: Path,
    context_files: list[Path],
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> Path:
    """FR-23/FR-24 — produce ``.workflow/review.md``."""
    srs = context_files[0] if context_files else worktree / WORKFLOW_DIR / "srs.md"
    plan = context_files[1] if len(context_files) > 1 else worktree / WORKFLOW_DIR / "plan.md"

    artifact = worktree / WORKFLOW_DIR / "review.md"
    scratch = worktree / WORKFLOW_DIR / ".review-last-message.txt"

    spec = adapter.command(  # type: ignore[attr-defined]
        HarnessOperation.REVIEW,
        worktree=worktree,
        prompt=review_prompt(srs, plan),
        output_file=scratch,
    )

    events = await collect(spec, adapter, timeout=timeout)
    result = await _write_artifact(spec, adapter, events, artifact)
    scratch.unlink(missing_ok=True)
    log.info(
        "harness.review_complete",
        harness=getattr(adapter, "name", "unknown"),
        artifact=str(result),
    )
    return result


async def run_as_built_operation(
    adapter: object,
    *,
    worktree: Path,
    context_files: list[Path],
    merged_diff: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> Path:
    """Produce ``.workflow/as-built.md`` after the pull request merges.

    Read-only like the plan and review operations, and held to the same FR-16
    assertion: reconciling the record must not quietly edit the thing it is
    reconciling.
    """
    from ..services.review import TRIAGE_FILENAME

    srs = context_files[0] if context_files else worktree / WORKFLOW_DIR / "srs.md"
    plan = context_files[1] if len(context_files) > 1 else worktree / WORKFLOW_DIR / "plan.md"
    review = (
        context_files[2] if len(context_files) > 2 else worktree / WORKFLOW_DIR / "review.md"
    )
    triage = worktree / WORKFLOW_DIR / TRIAGE_FILENAME

    artifact = worktree / WORKFLOW_DIR / f"{HarnessOperation.AS_BUILT.value}.md"
    scratch = worktree / WORKFLOW_DIR / ".as-built-last-message.txt"

    spec = adapter.command(  # type: ignore[attr-defined]
        HarnessOperation.AS_BUILT,
        worktree=worktree,
        prompt=as_built_prompt(
            srs, plan, review, triage=str(triage), merged_diff=merged_diff
        ),
        output_file=scratch,
    )

    events = await collect(spec, adapter, timeout=timeout)

    offenders = await changed_outside_workflow(worktree)
    if offenders:
        raise SourceModified(offenders)

    result = await _write_artifact(spec, adapter, events, artifact)
    scratch.unlink(missing_ok=True)
    log.info(
        "harness.as_built_complete",
        harness=getattr(adapter, "name", "unknown"),
        artifact=str(result),
        events=len(events),
    )
    return result


async def stream_operation(
    adapter: object,
    operation: HarnessOperation,
    *,
    prompt_file: Path,
    worktree: Path,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> AsyncIterator[RunEvent]:
    """FR-18 — run an editing operation, streaming progress."""
    from .prompts import implement_prompt

    srs = worktree / WORKFLOW_DIR / "srs.md"
    spec = adapter.command(  # type: ignore[attr-defined]
        operation,
        worktree=worktree,
        prompt=implement_prompt(prompt_file, srs),
        output_file=None,
    )
    async for event in stream_command(spec, adapter, timeout=timeout):
        yield event

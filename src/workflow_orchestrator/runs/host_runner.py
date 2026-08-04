"""Host-subprocess runner — the fallback when Docker is unavailable.

This drops NFR-1's container isolation: the harness runs as the current user with
the machine's full filesystem visible. It exists so the orchestrator is usable
before an agent image is built, and is gated behind
``WORKFLOW_ALLOW_HOST_RUNNER``.

The handle is ``host:<pid>:<start_time_ticks>``. Recording the process start time
alongside the PID matters: PIDs are recycled, so after a reboot a bare PID could
resolve to an unrelated process and a dead run would look alive.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import signal
from pathlib import Path

from ..config import Settings
from ..logging import get_logger
from .base import ExitInfo, LaunchSpec, RunHandle, RunState

log = get_logger(__name__)

BACKEND = "host"


def exit_path_for(log_path: Path) -> Path:
    """Sidecar holding the harness's exit status (see ``launch``)."""
    return log_path.with_suffix(".exit")


def _read_exit_code(log_path: Path) -> int | None:
    exit_file = exit_path_for(log_path)
    if not exit_file.exists():
        return None
    raw = exit_file.read_text(encoding="utf-8", errors="replace").strip()
    return int(raw) if raw.isdigit() else None


def _start_ticks(pid: int) -> str:
    """A value that changes if the PID is reused. Empty when unavailable."""
    try:
        import subprocess

        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip().replace(" ", "_") if result.returncode == 0 else ""
    except Exception:  # pragma: no cover - defensive
        return ""


class HostRunner:
    name = BACKEND

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def available(self) -> bool:
        return bool(self._settings.WORKFLOW_ALLOW_HOST_RUNNER)

    async def launch(self, spec: LaunchSpec) -> RunHandle:
        spec.log_path.parent.mkdir(parents=True, exist_ok=True)
        spec.log_path.touch()

        exit_file = exit_path_for(spec.log_path)
        exit_file.unlink(missing_ok=True)

        command = " ".join(shlex.quote(a) for a in spec.argv)
        if spec.stdin is not None:
            prompt_file = spec.log_path.with_suffix(".prompt.txt")
            prompt_file.write_text(spec.stdin, encoding="utf-8")
            command += f" < {shlex.quote(str(prompt_file))}"
        command += f" > {shlex.quote(str(spec.log_path))} 2>&1"
        # A detached process's exit status is unobservable from a PID poll, so
        # the wrapper records it. Without this a harness that exits non-zero
        # would be reported as a successful run.
        command += f"; printf %s $? > {shlex.quote(str(exit_file))}"

        env = {**os.environ, **spec.env}

        # start_new_session detaches the child into its own process group, so a
        # SIGKILL of the server (AC-4) does not take the harness down with it.
        process = await asyncio.create_subprocess_exec(
            "/bin/sh",
            "-c",
            command,
            cwd=str(spec.worktree),
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )

        handle = RunHandle(f"{BACKEND}:{process.pid}:{_start_ticks(process.pid)}")
        log.info(
            "run.launched",
            backend=BACKEND,
            handle=handle.value,
            log_path=str(spec.log_path),
        )
        return handle

    def _parse(self, handle: RunHandle) -> tuple[int, str] | None:
        parts = handle.value.split(":", 2)
        if len(parts) < 2 or parts[0] != BACKEND:
            return None
        try:
            return int(parts[1]), (parts[2] if len(parts) > 2 else "")
        except ValueError:
            return None

    async def poll(self, handle: RunHandle, log_path: Path | None = None) -> ExitInfo:
        parsed = self._parse(handle)
        if parsed is None:
            return ExitInfo(state=RunState.GONE)
        pid, recorded_ticks = parsed

        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            code = _read_exit_code(log_path) if log_path else None
            return ExitInfo(state=RunState.EXITED, exit_code=code)
        except PermissionError:
            # Exists but is owned by someone else — PID was recycled.
            return ExitInfo(state=RunState.GONE)

        # Guard against PID reuse: a live PID whose start time differs is a
        # different process, so the run we care about is gone.
        if recorded_ticks:
            current = _start_ticks(pid)
            if current and current != recorded_ticks:
                return ExitInfo(state=RunState.GONE)

        return ExitInfo(state=RunState.RUNNING)

    async def terminate(self, handle: RunHandle) -> None:
        parsed = self._parse(handle)
        if parsed is None:
            return
        pid, _ = parsed
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                # Signal the whole process group; the harness spawns children.
                os.killpg(os.getpgid(pid), sig)
            except (ProcessLookupError, PermissionError):
                return
            await asyncio.sleep(2)
            if (await self.poll(handle)).state is not RunState.RUNNING:
                return
        log.warning("run.terminate_incomplete", handle=handle.value)

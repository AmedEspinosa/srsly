"""Docker runner — the sandboxed path required by NFR-1.

The container runs as a non-root user, with **only** the session worktree
mounted, and no other host path visible. Output is redirected to
``.workflow/run-<id>.log`` inside the mounted worktree, so the log lands on the
host and the supervisor tails it exactly as it does for a host run.

The handle is the container ID, persisted immediately after ``docker run -d`` so
a crash between launch and first poll is still recoverable (FR-33, NFR-4).
"""

from __future__ import annotations

import shlex
from pathlib import Path

from ..config import Settings
from ..logging import get_logger
from ..services.process import run_command
from .base import ExitInfo, LaunchSpec, RunHandle, RunState

log = get_logger(__name__)

BACKEND = "docker"
CONTAINER_WORKDIR = "/work"
CONTAINER_USER = "agent"

#: Which credentials to forward is harness knowledge and arrives on
#: ``LaunchSpec.forward_env`` (AC-9). Bedrock credentials are never forwarded:
#: the requirements engine runs in-process on the host, so the sandbox has no
#: use for them (NFR-1, NFR-2).


class DockerRunner:
    name = BACKEND

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def available(self) -> bool:
        if not self._settings.DOCKER_IMAGE_AGENT:
            return False
        try:
            result = await run_command(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=15)
        except (FileNotFoundError, TimeoutError):
            return False
        return result.ok and bool(result.stdout.strip())

    def build_argv(self, spec: LaunchSpec) -> list[str]:
        """Construct ``docker run``. Split out so it is assertable in tests."""
        image = self._settings.DOCKER_IMAGE_AGENT or ""

        # The log path is on the host inside the worktree; translate it to the
        # container's view of the same file.
        relative_log = spec.log_path.relative_to(spec.worktree)
        container_log = f"{CONTAINER_WORKDIR}/{relative_log.as_posix()}"

        inner = " ".join(shlex.quote(a) for a in spec.argv)
        if spec.stdin is not None:
            relative_prompt = spec.log_path.with_suffix(".prompt.txt").relative_to(
                spec.worktree
            )
            inner += f" < {shlex.quote(f'{CONTAINER_WORKDIR}/{relative_prompt.as_posix()}')}"
        inner += f" > {shlex.quote(container_log)} 2>&1"

        argv = [
            "docker",
            "run",
            "--detach",
            "--user",
            CONTAINER_USER,
            # NFR-1: the session worktree is the only host path mounted.
            "--volume",
            f"{spec.worktree}:{CONTAINER_WORKDIR}",
            "--workdir",
            CONTAINER_WORKDIR,
            # Defence in depth on top of the non-root user.
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
        ]
        for name in spec.forward_env:
            argv += ["--env", name]
        for name, value in spec.env.items():
            argv += ["--env", f"{name}={value}"]

        argv += [image, "/bin/sh", "-c", inner]
        return argv

    async def launch(self, spec: LaunchSpec) -> RunHandle:
        spec.log_path.parent.mkdir(parents=True, exist_ok=True)
        spec.log_path.touch()
        if spec.stdin is not None:
            spec.log_path.with_suffix(".prompt.txt").write_text(
                spec.stdin, encoding="utf-8"
            )

        result = await run_command(self.build_argv(spec), timeout=120)
        if not result.ok:
            raise RuntimeError(
                f"docker run failed: {(result.stderr or result.stdout).strip()}"
            )

        container_id = result.stdout.strip().splitlines()[-1].strip()
        handle = RunHandle(container_id)
        log.info(
            "run.launched",
            backend=BACKEND,
            handle=container_id[:12],
            log_path=str(spec.log_path),
        )
        return handle

    async def poll(self, handle: RunHandle, log_path: Path | None = None) -> ExitInfo:
        result = await run_command(
            [
                "docker",
                "inspect",
                "--format",
                "{{.State.Running}} {{.State.ExitCode}}",
                handle.value,
            ],
            timeout=30,
        )
        if not result.ok:
            # No such container — pruned, or never started.
            return ExitInfo(state=RunState.GONE)

        parts = result.stdout.strip().split()
        running = parts[0].lower() == "true" if parts else False
        exit_code = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
        if running:
            return ExitInfo(state=RunState.RUNNING)
        return ExitInfo(state=RunState.EXITED, exit_code=exit_code)

    async def terminate(self, handle: RunHandle) -> None:
        await run_command(["docker", "stop", "--time", "10", handle.value], timeout=60)
        log.info("run.terminated", backend=BACKEND, handle=handle.value[:12])

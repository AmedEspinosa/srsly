"""Runner backend interface.

Both backends (Docker and host subprocess) share one shape so the supervisor
never branches on which is in use:

* The harness process is launched **detached** — it outlives the FastAPI server
  (FR-31).
* Its stdout/stderr is redirected to ``.workflow/run-<run_id>.log`` inside the
  session worktree (FR-20). The worktree is on the host in both cases, so the
  supervisor tails a plain file rather than a backend-specific stream.
* A backend-opaque handle string is persisted to ``runs.container_id``, which is
  all reattach needs after a restart (FR-32, NFR-4).

Tailing a file rather than ``docker logs``/pipes is what makes reattach uniform:
replaying from offset 0 reproduces everything the run has emitted so far, and
following from the end continues live.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable


class RunState(str, enum.Enum):
    RUNNING = "running"
    EXITED = "exited"
    GONE = "gone"  # handle no longer resolvable (container pruned, PID reused)


@dataclass(frozen=True)
class LaunchSpec:
    """Everything a backend needs to start a detached harness process."""

    argv: tuple[str, ...]
    worktree: Path
    log_path: Path
    #: Prompt content; written to a file and piped in on stdin.
    stdin: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    #: Names of host environment variables to forward into the sandbox.
    #:
    #: Supplied by the harness adapter, because which credentials a backend
    #: needs is backend knowledge (AC-9). The runner forwards names only — no
    #: value is ever read or logged here (NFR-2).
    forward_env: tuple[str, ...] = ()


@dataclass(frozen=True)
class RunHandle:
    """Backend-opaque identifier persisted to ``runs.container_id``."""

    value: str

    @property
    def backend(self) -> str:
        return self.value.split(":", 1)[0] if ":" in self.value else "docker"


@dataclass(frozen=True)
class ExitInfo:
    state: RunState
    exit_code: int | None = None


@runtime_checkable
class RunnerBackend(Protocol):
    name: str

    async def available(self) -> bool:
        """Whether this backend can be used right now."""
        ...

    async def launch(self, spec: LaunchSpec) -> RunHandle:
        """Start the process detached and return its handle."""
        ...

    async def poll(self, handle: RunHandle, log_path: Path | None = None) -> ExitInfo:
        """Current state of a previously launched process.

        ``log_path`` lets a backend locate side-channel state it wrote next to
        the log (the host runner records the exit status there, since a detached
        process's status is not observable from a PID poll).
        """
        ...

    async def terminate(self, handle: RunHandle) -> None:
        """Stop the process — used by the timeout and cost ceilings (FR-21)."""
        ...

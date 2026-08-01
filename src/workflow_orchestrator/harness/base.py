"""Harness adapter interface — SRS §4.4.

    class HarnessAdapter(Protocol):
        async def plan(self, prompt_file, worktree) -> Path
        async def implement(self, plan_file, worktree) -> AsyncIterator[RunEvent]
        async def review(self, target, worktree, context_files) -> Path

Both concrete adapters translate their CLI's JSONL event stream into
:class:`RunEvent`, which is the only shape the supervisor and the SSE layer
understand.
"""

from __future__ import annotations

import enum
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable


class HarnessOperation(str, enum.Enum):
    PLAN = "plan"
    IMPLEMENT = "implement"
    REVIEW = "review"


@dataclass
class RunEvent:
    """A single unit of harness output — SRS §4.4."""

    event_type: str
    text: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    token_count: int | None = None
    cost_usd: float | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "event_type": self.event_type,
            "text": self.text,
            "timestamp": self.timestamp.isoformat().replace("+00:00", "Z"),
            "token_count": self.token_count,
            "cost_usd": self.cost_usd,
        }


@dataclass(frozen=True)
class CommandSpec:
    """A fully-resolved harness invocation.

    Kept separate from execution so the supervisor can run it in a container or
    as a host subprocess, and so tests can assert on the argv without spawning
    anything.
    """

    argv: tuple[str, ...]
    cwd: Path
    env: dict[str, str] = field(default_factory=dict)
    #: Prompt delivered on stdin rather than as an argv entry.
    #:
    #: Both CLIs expose variadic options (``--allowedTools <tools...>``,
    #: ``--add-dir <dirs...>``) that greedily consume every following non-option
    #: token — including a trailing prompt, which then dies with "Input must be
    #: provided either through stdin or as a prompt argument". Feeding the prompt
    #: on stdin sidesteps argv ordering entirely and has no length limit.
    stdin: str | None = None
    #: File the harness writes its final message to, when the CLI supports it.
    output_file: Path | None = None
    #: True when the invocation cannot modify the worktree (FR-16).
    read_only: bool = False


@runtime_checkable
class HarnessAdapter(Protocol):
    """SRS §4.4."""

    name: str
    #: Host environment variable *names* the sandbox must receive to
    #: authenticate. Declared per adapter so the runners stay backend-agnostic.
    credential_env: tuple[str, ...]
    #: Whether reported costs are running session totals (True) or
    #: per-event increments (False). The meter cannot infer this.
    cost_is_cumulative: bool

    def command(
        self,
        operation: HarnessOperation,
        *,
        worktree: Path,
        prompt: str,
        output_file: Path | None = None,
    ) -> CommandSpec:
        """Build the CLI invocation for ``operation``."""
        ...

    def parse_line(self, line: str) -> RunEvent | None:
        """Translate one line of CLI output into a RunEvent, or None to skip."""
        ...

    async def plan(self, prompt_file: Path, worktree: Path) -> Path:
        """Produce ``.workflow/plan.md`` without modifying source files (FR-15/16)."""
        ...

    async def implement(
        self, plan_file: Path, worktree: Path
    ) -> AsyncIterator[RunEvent]:
        """Execute the plan, streaming progress (FR-18)."""
        ...

    async def review(
        self, target: Path, worktree: Path, context_files: list[Path]
    ) -> Path:
        """Produce ``.workflow/review.md`` (FR-23/24)."""
        ...


def iso(timestamp: datetime | None = None) -> str:
    return (timestamp or datetime.now(UTC)).isoformat().replace("+00:00", "Z")

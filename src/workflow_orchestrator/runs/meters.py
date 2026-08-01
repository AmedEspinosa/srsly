"""Run limits — SRS FR-21.

    "Every run MUST carry a configurable hard wall-clock timeout (default: 90
     minutes) and a configurable token/cost ceiling (default: $5.00 USD).
     Exceeding either MUST terminate the run and record the run as `timed_out`
     or `cost_exceeded`."

Cost accumulation differs by backend and both are handled here rather than in the
supervisor: Claude Code reports ``total_cost_usd`` on its result event, while
Codex reports token counts that its adapter converts to an estimate. Either way
the meter only ever sees ``RunEvent.cost_usd``.

Backends differ in what a reported cost *means*, and the difference cannot be
inferred safely:

* Claude Code's ``result`` event carries the **cumulative** session cost. Summing
  successive reports would trip the ceiling far too early.
* Codex's ``turn.completed`` usage covers **that turn only**. Taking the maximum
  would under-count a multi-turn run and never trip the ceiling.

Both shapes look identical from a single sample (a repeated ``0.10`` is either a
flat cumulative total or three increments), so the adapter declares its semantics
via ``cost_is_cumulative`` rather than the meter guessing.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..harness.base import RunEvent
from ..models import RunStatus


@dataclass
class LimitBreach:
    status: RunStatus
    reason: str


@dataclass
class RunMeter:
    timeout_seconds: float
    cost_ceiling_usd: float
    #: True when the harness reports a running session total on every event
    #: (Claude Code); False when each report is an increment (Codex).
    cost_is_cumulative: bool = False
    started_monotonic: float = field(default_factory=time.monotonic)

    #: Totals derived from the events seen so far.
    _observed_cost: float = 0.0
    _observed_tokens: int = 0

    #: Floor carried across a reattach, so replaying a log cannot lose spend.
    _resumed_cost: float = 0.0
    _resumed_tokens: int = 0

    @property
    def cost_usd(self) -> float:
        """Spend to charge against the ceiling.

        A floor rather than a starting sum: reattach replays the whole log, so
        adding the persisted total to the replayed events would double-count.
        """
        return max(self._observed_cost, self._resumed_cost)

    @property
    def tokens_used(self) -> int:
        return max(self._observed_tokens, self._resumed_tokens)

    def observe(self, event: RunEvent) -> None:
        if event.cost_usd is not None and event.cost_usd >= 0:
            if self.cost_is_cumulative:
                self._observed_cost = max(self._observed_cost, event.cost_usd)
            else:
                self._observed_cost += event.cost_usd

        if event.token_count is not None and event.token_count >= 0:
            if self.cost_is_cumulative:
                self._observed_tokens = max(self._observed_tokens, event.token_count)
            else:
                self._observed_tokens += event.token_count

    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self.started_monotonic

    def check(self) -> LimitBreach | None:
        """The breach to act on, or None while the run is within limits."""
        if self.elapsed_seconds >= self.timeout_seconds:
            return LimitBreach(
                status=RunStatus.TIMED_OUT,
                reason=(
                    f"wall-clock timeout of {self.timeout_seconds / 60:.0f} minutes "
                    f"exceeded"
                ),
            )
        if self.cost_ceiling_usd > 0 and self.cost_usd >= self.cost_ceiling_usd:
            return LimitBreach(
                status=RunStatus.COST_EXCEEDED,
                reason=(
                    f"cost ceiling of ${self.cost_ceiling_usd:.2f} reached "
                    f"(spent ${self.cost_usd:.4f})"
                ),
            )
        return None

    def resume_from(self, *, tokens_used: int, cost_usd: float) -> None:
        """Restore accounting after a reattach so limits are not reset (NFR-4)."""
        self._resumed_cost = max(self._resumed_cost, cost_usd)
        self._resumed_tokens = max(self._resumed_tokens, tokens_used)

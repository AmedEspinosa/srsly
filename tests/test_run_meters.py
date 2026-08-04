"""Run limits — FR-21."""

from __future__ import annotations

import time

import pytest

from workflow_orchestrator.harness.base import RunEvent
from workflow_orchestrator.models import RunStatus
from workflow_orchestrator.runs.meters import RunMeter


def meter(**kwargs: object) -> RunMeter:
    defaults = {"timeout_seconds": 3600.0, "cost_ceiling_usd": 5.00}
    defaults.update(kwargs)
    return RunMeter(**defaults)  # type: ignore[arg-type]


def test_within_limits_returns_none() -> None:
    assert meter().check() is None


def test_cost_ceiling_trips() -> None:
    m = meter(cost_ceiling_usd=0.01)
    m.observe(RunEvent(event_type="result", text="", cost_usd=0.02))
    breach = m.check()
    assert breach is not None
    assert breach.status is RunStatus.COST_EXCEEDED
    assert "0.01" in breach.reason


def test_timeout_trips() -> None:
    m = meter(timeout_seconds=0.0)
    breach = m.check()
    assert breach is not None
    assert breach.status is RunStatus.TIMED_OUT


def test_timeout_wins_over_cost_when_both_breach() -> None:
    m = meter(timeout_seconds=0.0, cost_ceiling_usd=0.01)
    m.observe(RunEvent(event_type="result", text="", cost_usd=99.0))
    assert m.check().status is RunStatus.TIMED_OUT


def test_cumulative_cost_reports_are_not_double_counted() -> None:
    """Claude Code reports the whole session cost on every result event.

    Summing those would trip the ceiling far too early.
    """
    m = meter(cost_is_cumulative=True)
    for total in (0.10, 0.25, 0.40):
        m.observe(RunEvent(event_type="result", text="", cost_usd=total))
    assert m.cost_usd == pytest.approx(0.40)


def test_incremental_cost_reports_accumulate() -> None:
    """Codex reports per-turn usage, so a multi-turn run must sum.

    Note both shapes emit an identical-looking 0.10 here — which is exactly why
    the adapter declares its semantics instead of the meter inferring them.
    """
    m = meter(cost_is_cumulative=False)
    for _ in range(10):
        m.observe(RunEvent(event_type="result", text="", cost_usd=0.10))
    assert m.cost_usd == pytest.approx(1.0)


def test_identical_reports_are_read_per_backend_semantics() -> None:
    events = [RunEvent(event_type="result", text="", cost_usd=0.10)] * 3

    cumulative = meter(cost_is_cumulative=True)
    incremental = meter(cost_is_cumulative=False)
    for event in events:
        cumulative.observe(event)
        incremental.observe(event)

    assert cumulative.cost_usd == pytest.approx(0.10)
    assert incremental.cost_usd == pytest.approx(0.30)


def test_replay_after_reattach_does_not_double_count_incremental() -> None:
    """NFR-4 — reattach replays the log, so the floor must not be added to it."""
    m = meter(cost_is_cumulative=False, cost_ceiling_usd=10.0)
    m.resume_from(tokens_used=100, cost_usd=0.30)
    # Replaying the same three increments the persisted total came from.
    for _ in range(3):
        m.observe(RunEvent(event_type="result", text="", cost_usd=0.10))
    assert m.cost_usd == pytest.approx(0.30)


def test_negative_and_missing_values_are_ignored() -> None:
    m = meter()
    m.observe(RunEvent(event_type="text_delta", text="hi"))
    m.observe(RunEvent(event_type="result", text="", cost_usd=-1.0, token_count=-5))
    assert m.cost_usd == 0.0
    assert m.tokens_used == 0


def test_zero_ceiling_disables_the_cost_limit() -> None:
    m = meter(cost_ceiling_usd=0.0)
    m.observe(RunEvent(event_type="result", text="", cost_usd=1000.0))
    assert m.check() is None


def test_resume_restores_accounting_after_reattach() -> None:
    """NFR-4 — a reattached run must not restart its spend from zero."""
    m = meter(cost_ceiling_usd=1.00)
    m.resume_from(tokens_used=5000, cost_usd=0.95)

    # Before any event is replayed the floor already protects the ceiling, so a
    # reattached run cannot quietly get a fresh budget.
    assert m.cost_usd == 0.95
    assert m.tokens_used == 5000
    assert m.check() is None


def test_reattached_run_trips_the_ceiling_on_new_spend() -> None:
    """The real reattach flow: replay the log, then keep accumulating."""
    m = meter(cost_ceiling_usd=1.00, cost_is_cumulative=False)
    m.resume_from(tokens_used=5000, cost_usd=0.95)

    # Replay reconstructs the persisted total from the log…
    for _ in range(19):
        m.observe(RunEvent(event_type="result", text="", cost_usd=0.05))
    assert m.cost_usd == pytest.approx(0.95)
    assert m.check() is None

    # …then genuinely new spend pushes it over.
    m.observe(RunEvent(event_type="result", text="", cost_usd=0.10))
    assert m.check().status is RunStatus.COST_EXCEEDED


def test_elapsed_advances() -> None:
    m = meter()
    start = m.elapsed_seconds
    time.sleep(0.01)
    assert m.elapsed_seconds > start

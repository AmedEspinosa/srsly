"""Phase state machine — FR-5, FR-14."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from workflow_orchestrator.models import Phase
from workflow_orchestrator.phases import (
    PHASE_ORDER,
    InvalidTransition,
    PhaseNotReady,
    check_can_approve,
    check_can_enter,
    missing_prerequisite,
    next_phase,
    prerequisites_for,
    previous_phase,
)


@dataclass
class FakeApproval:
    phase: str


def approvals(*phases: Phase) -> list[FakeApproval]:
    return [FakeApproval(phase=p.value) for p in phases]


def test_phase_order_matches_srs() -> None:
    assert [p.value for p in PHASE_ORDER] == [
        "qa",
        "srs",
        "plan",
        "implement",
        "review",
        "merge",
        "completed",
    ]


def test_next_and_previous() -> None:
    assert next_phase(Phase.QA) is Phase.SRS
    assert next_phase(Phase.MERGE) is Phase.COMPLETED
    assert next_phase(Phase.COMPLETED) is Phase.COMPLETED
    assert previous_phase(Phase.PLAN) is Phase.SRS
    # FR-13: rejecting out of QA stays at QA, the entry point.
    assert previous_phase(Phase.QA) is Phase.QA


def test_qa_is_not_a_prerequisite() -> None:
    """FR-5 exempts ``qa`` — its artifact is approved under ``srs`` (FR-14)."""
    assert Phase.QA not in prerequisites_for(Phase.PLAN)
    assert prerequisites_for(Phase.PLAN) == (Phase.SRS,)
    assert prerequisites_for(Phase.MERGE) == (
        Phase.SRS,
        Phase.PLAN,
        Phase.IMPLEMENT,
        Phase.REVIEW,
    )


def test_srs_needs_no_prior_approval() -> None:
    assert missing_prerequisite(Phase.SRS, []) is None
    check_can_approve(Phase.SRS, Phase.SRS, [])


def test_skipping_a_gate_reports_the_skipped_gate() -> None:
    """AC-2: approving ``plan`` with no ``srs`` approval names ``srs``."""
    with pytest.raises(PhaseNotReady) as exc:
        check_can_approve(Phase.SRS, Phase.PLAN, [])
    assert exc.value.required_approval is Phase.SRS
    assert exc.value.as_payload() == {
        "error": "phase_not_ready",
        "required_approval": "srs",
    }


def test_skipping_two_gates_reports_the_earliest() -> None:
    with pytest.raises(PhaseNotReady) as exc:
        check_can_approve(Phase.SRS, Phase.REVIEW, approvals(Phase.SRS))
    assert exc.value.required_approval is Phase.PLAN


def test_approving_out_of_turn_with_gates_met_is_an_invalid_transition() -> None:
    # Prerequisites for ``plan`` are satisfied, but the session sits at ``implement``.
    with pytest.raises(InvalidTransition):
        check_can_approve(Phase.IMPLEMENT, Phase.PLAN, approvals(Phase.SRS, Phase.PLAN))


def test_completed_cannot_be_approved_directly() -> None:
    with pytest.raises(InvalidTransition):
        check_can_approve(Phase.MERGE, Phase.COMPLETED, approvals(*PHASE_ORDER))


def test_full_walk_through_every_gate() -> None:
    recorded: list[FakeApproval] = []
    current = Phase.SRS
    for phase in (Phase.SRS, Phase.PLAN, Phase.IMPLEMENT, Phase.REVIEW, Phase.MERGE):
        check_can_approve(current, phase, recorded)
        recorded.append(FakeApproval(phase=phase.value))
        current = next_phase(phase)
    assert current is Phase.COMPLETED


def test_check_can_enter_gates_downstream_phases() -> None:
    check_can_enter(Phase.PLAN, approvals(Phase.SRS))
    with pytest.raises(PhaseNotReady) as exc:
        check_can_enter(Phase.IMPLEMENT, approvals(Phase.SRS))
    assert exc.value.required_approval is Phase.PLAN

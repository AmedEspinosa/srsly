"""Phase state machine — SRS FR-5, FR-14, FR-17, FR-22, FR-26.

A session moves ``qa -> srs -> plan -> implement -> review -> merge -> completed``.
No phase may be entered without a recorded approval of the preceding phase's
artifact, except ``qa`` which is the entry point.

The single source of truth for both the order and the artifact each phase
produces lives here, so the gate cannot drift from the UI or the API.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import Phase

#: Ordered phase progression (FR-5).
PHASE_ORDER: tuple[Phase, ...] = (
    Phase.QA,
    Phase.SRS,
    Phase.PLAN,
    Phase.IMPLEMENT,
    Phase.REVIEW,
    Phase.MERGE,
    Phase.COMPLETED,
)

_INDEX: dict[Phase, int] = {phase: i for i, phase in enumerate(PHASE_ORDER)}

#: Phases that require a recorded approval before the *next* phase may be
#: entered. ``qa`` is excluded: FR-5 makes it the entry point, and the artifact
#: it produces (``srs.md``) is approved under the ``srs`` phase per FR-14.
#: This is what makes AC-2 report ``required_approval: "srs"`` rather than
#: ``"qa"`` when the ``plan`` phase is requested too early.
GATED_PHASES: tuple[Phase, ...] = tuple(p for p in PHASE_ORDER if p is not Phase.QA)

#: Relative path of the artifact a phase must produce before it can be approved.
#: ``qa`` produces ``srs.md`` (FR-12), which the ``srs`` phase then gates on.
PHASE_ARTIFACT: dict[Phase, str] = {
    Phase.QA: ".workflow/srs.md",
    Phase.SRS: ".workflow/srs.md",
    Phase.PLAN: ".workflow/plan.md",
    Phase.IMPLEMENT: ".workflow/diff.patch",
    Phase.REVIEW: ".workflow/review.md",
    Phase.MERGE: ".workflow/pr.json",
}


class PhaseError(Exception):
    """Base class for phase-transition failures."""

    error_code = "phase_error"

    def as_payload(self) -> dict[str, object]:
        return {"error": self.error_code}


@dataclass
class PhaseNotReady(PhaseError):
    """AC-2: a phase was entered without the preceding approval."""

    required_approval: Phase

    error_code = "phase_not_ready"

    def __str__(self) -> str:  # pragma: no cover - message only
        return f"approval for phase {self.required_approval.value!r} is required first"

    def as_payload(self) -> dict[str, object]:
        return {"error": self.error_code, "required_approval": self.required_approval.value}


@dataclass
class InvalidTransition(PhaseError):
    current: Phase
    requested: Phase

    error_code = "invalid_transition"

    def __str__(self) -> str:  # pragma: no cover - message only
        return f"cannot move from {self.current.value!r} to {self.requested.value!r}"

    def as_payload(self) -> dict[str, object]:
        return {
            "error": self.error_code,
            "current_phase": self.current.value,
            "requested_phase": self.requested.value,
        }


def index_of(phase: Phase) -> int:
    return _INDEX[phase]


def next_phase(phase: Phase) -> Phase:
    """The phase that follows ``phase``. ``completed`` is absorbing."""
    i = _INDEX[phase]
    if i + 1 >= len(PHASE_ORDER):
        return Phase.COMPLETED
    return PHASE_ORDER[i + 1]


def previous_phase(phase: Phase) -> Phase:
    """The phase a rejection returns to. ``qa`` is the entry point (FR-13)."""
    i = _INDEX[phase]
    if i == 0:
        return Phase.QA
    return PHASE_ORDER[i - 1]


def approved_phases(approvals: object) -> set[Phase]:
    """Extract the set of approved phases from an iterable of Approval rows."""
    result: set[Phase] = set()
    for approval in approvals:  # type: ignore[union-attr]
        try:
            result.add(Phase(approval.phase))
        except ValueError:  # pragma: no cover - defensive
            continue
    return result


def prerequisites_for(target: Phase) -> tuple[Phase, ...]:
    """Approvals that must already exist before ``target`` may be entered."""
    return tuple(p for p in GATED_PHASES if _INDEX[p] < _INDEX[target])


def missing_prerequisite(target: Phase, approvals: object) -> Phase | None:
    """The earliest unmet prerequisite for ``target``, or None."""
    already = approved_phases(approvals)
    for earlier in prerequisites_for(target):
        if earlier not in already:
            return earlier
    return None


def check_can_approve(current: Phase, requested: Phase, approvals: object) -> None:
    """Validate that ``requested`` may be approved right now.

    Prerequisites are checked *before* the current-phase match so that a request
    which skips a gate reports the gate it skipped (AC-2 expects
    ``required_approval: "srs"`` when ``plan`` is approved out of turn), rather
    than a generic transition error.
    """
    if requested is Phase.COMPLETED:
        raise InvalidTransition(current=current, requested=requested)

    unmet = missing_prerequisite(requested, approvals)
    if unmet is not None:
        raise PhaseNotReady(required_approval=unmet)

    if requested is not current:
        raise InvalidTransition(current=current, requested=requested)


def check_can_enter(target: Phase, approvals: object) -> None:
    """Validate that ``target`` may be entered, given recorded approvals.

    Used by phase-triggering endpoints (start plan run, start implement run, …)
    rather than by the approval endpoint itself.
    """
    unmet = missing_prerequisite(target, approvals)
    if unmet is not None:
        raise PhaseNotReady(required_approval=unmet)


def artifact_for(phase: Phase) -> str | None:
    return PHASE_ARTIFACT.get(phase)

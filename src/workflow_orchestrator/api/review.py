"""Review phase endpoints — SRS FR-23, FR-24, FR-25, FR-26."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..harness import get_adapter
from ..harness.base import HarnessOperation
from ..harness.prompts import follow_up_prompt
from ..harness.runner import HarnessError
from ..logging import get_logger
from ..models import Harness, Phase
from ..phases import PhaseError, check_can_enter
from ..schemas import RunOut
from ..services import review as review_service
from ..services import workflow
from ..runs.supervisor import NoRunnerAvailable, SupervisorError, get_supervisor
from .deps import AppSettings, CurrentSession, DbSession, phase_error_response

log = get_logger(__name__)

router = APIRouter(tags=["review"])


async def _project_or_404(db: DbSession, session: CurrentSession):
    project = await workflow.get_project(db, session.project_id)
    if project is None:  # pragma: no cover - FK guarantees this
        raise HTTPException(status_code=404, detail="project not found")
    return project


def _reviewing_harness(session: CurrentSession) -> Harness:
    """FR-23 — the harness *not* used for implementation.

    ``harness_review`` is already constrained to differ from
    ``harness_implement`` by the DB CHECK and the API layer, so this is a read
    rather than a computation; deriving it from ``other()`` as well would let the
    two disagree silently.
    """
    return Harness(session.harness_review)


@router.post("/sessions/{session_id}/review/run")
async def run_review(
    db: DbSession, settings: AppSettings, session: CurrentSession
) -> dict[str, object]:
    """FR-23/FR-24 — cross-harness review producing ``.workflow/review.md``."""
    project = await _project_or_404(db, session)

    try:
        check_can_enter(Phase.REVIEW, session.approvals)
    except PhaseError as exc:
        return phase_error_response(exc)  # type: ignore[return-value]

    worktree = workflow.session_worktree(project, session)
    srs = worktree / ".workflow" / "srs.md"
    plan = worktree / ".workflow" / "plan.md"
    for required in (srs, plan):
        if not required.exists():
            raise HTTPException(
                status_code=422,
                detail={"error": "artifact_missing", "artifact_path": str(required)},
            )

    harness = _reviewing_harness(session)
    adapter = get_adapter(harness, settings)
    try:
        # Seeded with srs.md and plan.md as context (FR-23).
        review_path = await adapter.review(worktree, worktree, [srs, plan])
    except HarnessError as exc:
        log.error("review.harness_failed", session_id=session.id, error=str(exc))
        raise HTTPException(
            status_code=502, detail={"error": "harness_error", "detail": str(exc)}
        )

    await workflow.record_artifact(db, session, Phase.REVIEW, review_path)
    text = review_path.read_text(encoding="utf-8", errors="replace")
    findings = review_service.findings_with_triage(worktree, text)

    log.info(
        "review.complete",
        session_id=session.id,
        harness=harness.value,
        findings=len(findings),
    )
    return {
        "harness": harness.value,
        "path": str(review_path),
        "content": text,
        "findings": [f.to_dict() for f in findings],
    }


@router.get("/sessions/{session_id}/review/findings")
async def list_findings(
    db: DbSession, settings: AppSettings, session: CurrentSession
) -> dict[str, object]:
    """FR-25 — the triage view's data."""
    project = await _project_or_404(db, session)
    worktree = workflow.session_worktree(project, session)
    review_path = worktree / ".workflow" / "review.md"

    if not review_path.exists():
        return {"exists": False, "findings": [], "content": ""}

    text = review_path.read_text(encoding="utf-8", errors="replace")
    findings = review_service.findings_with_triage(worktree, text)
    # Close the loop on any follow-up run that has finished since the last read;
    # without this the outstanding count never falls.
    await review_service.reconcile_fixing(db, worktree, findings)
    return {
        "exists": True,
        "content": review_service.parse_review(text).prose,
        "findings": [f.to_dict() for f in findings],
        "outstanding": len(review_service.outstanding(findings)),
    }


@router.post("/sessions/{session_id}/review/findings/{finding_id}/dismiss")
async def dismiss_finding(
    db: DbSession, settings: AppSettings, session: CurrentSession, finding_id: str
) -> dict[str, object]:
    """FR-25 — each finding is individually dismissible."""
    project = await _project_or_404(db, session)
    worktree = workflow.session_worktree(project, session)

    review_service.set_finding_status(
        worktree, finding_id, review_service.STATUS_DISMISSED
    )
    return {"finding_id": finding_id, "status": review_service.STATUS_DISMISSED}


@router.post("/sessions/{session_id}/review/findings/{finding_id}/reopen")
async def reopen_finding(
    db: DbSession, settings: AppSettings, session: CurrentSession, finding_id: str
) -> dict[str, object]:
    project = await _project_or_404(db, session)
    worktree = workflow.session_worktree(project, session)
    review_service.set_finding_status(worktree, finding_id, review_service.STATUS_OPEN)
    return {"finding_id": finding_id, "status": review_service.STATUS_OPEN}


@router.post(
    "/sessions/{session_id}/review/findings/{finding_id}/fix", response_model=RunOut
)
async def fix_finding(
    db: DbSession, settings: AppSettings, session: CurrentSession, finding_id: str
) -> RunOut:
    """FR-25 — a finding is actionable: trigger a scoped follow-up implement run."""
    project = await _project_or_404(db, session)
    worktree = workflow.session_worktree(project, session)
    review_path = worktree / ".workflow" / "review.md"
    if not review_path.exists():
        raise HTTPException(
            status_code=422,
            detail={"error": "artifact_missing", "artifact_path": str(review_path)},
        )

    text = review_path.read_text(encoding="utf-8", errors="replace")
    findings = review_service.findings_with_triage(worktree, text)
    finding = next((f for f in findings if f.id == finding_id), None)
    if finding is None:
        raise HTTPException(status_code=404, detail="finding not found")

    # Release this request's transaction before launching: the supervisor writes
    # the run row in its own transaction, and SQLite permits one writer at a time.
    await db.commit()

    # The fix runs on the *implementing* harness, not the reviewing one.
    supervisor = get_supervisor(settings)
    try:
        run = await supervisor.start_run(
            session=session,
            worktree=worktree,
            phase=Phase.REVIEW,
            harness=Harness(session.harness_implement),
            operation=HarnessOperation.IMPLEMENT,
            prompt=follow_up_prompt(finding.as_prompt()),
        )
    except NoRunnerAvailable as exc:
        raise HTTPException(
            status_code=503, detail={"error": "no_runner_available", "detail": str(exc)}
        )
    except SupervisorError as exc:
        raise HTTPException(
            status_code=502, detail={"error": "run_launch_failed", "detail": str(exc)}
        )

    review_service.set_finding_status(
        worktree, finding_id, review_service.STATUS_FIXING, run_id=run.id
    )
    return RunOut.model_validate(run)

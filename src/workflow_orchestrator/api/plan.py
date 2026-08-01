"""Plan phase endpoints — SRS FR-15, FR-16, FR-17."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ..harness import get_adapter
from ..harness.runner import HarnessError, SourceModified
from ..logging import get_logger
from ..models import Harness, Phase
from ..phases import PhaseError, check_can_enter
from ..schemas import ArtifactOut
from ..services import workflow
from .deps import AppSettings, CurrentSession, DbSession, phase_error_response

log = get_logger(__name__)

router = APIRouter(tags=["plan"])


@router.post("/sessions/{session_id}/plan/run")
async def run_plan(
    request: Request, db: DbSession, settings: AppSettings, session: CurrentSession
) -> dict[str, object]:
    """FR-15 — invoke the implementing harness's ``plan`` operation."""
    project = await workflow.get_project(db, session.project_id)
    if project is None:  # pragma: no cover - FK guarantees this
        raise HTTPException(status_code=404, detail="project not found")

    # FR-17 — the plan phase cannot start until the SRS is approved.
    try:
        check_can_enter(Phase.PLAN, session.approvals)
    except PhaseError as exc:
        return phase_error_response(exc)  # type: ignore[return-value]

    worktree = workflow.session_worktree(project, session)
    srs = worktree / ".workflow" / "srs.md"
    if not srs.exists():
        raise HTTPException(
            status_code=422,
            detail={"error": "artifact_missing", "artifact_path": str(srs)},
        )

    adapter = get_adapter(Harness(session.harness_implement), settings)
    try:
        plan_path = await adapter.plan(srs, worktree)
    except SourceModified as exc:
        # FR-16 — a planning run must not touch source.
        log.error("plan.source_modified", session_id=session.id, paths=exc.paths)
        raise HTTPException(
            status_code=422,
            detail={"error": "source_modified", "paths": exc.paths},
        )
    except HarnessError as exc:
        log.error("plan.harness_failed", session_id=session.id, error=str(exc))
        raise HTTPException(
            status_code=502, detail={"error": "harness_error", "detail": str(exc)}
        )

    artifact = await workflow.record_artifact(db, session, Phase.PLAN, plan_path)
    return {
        "artifact": ArtifactOut.model_validate(artifact).model_dump(),
        "content": plan_path.read_text(encoding="utf-8", errors="replace"),
    }

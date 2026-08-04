"""Session endpoints — SRS FR-4..FR-7, FR-13, FR-44, FR-45, §4.3.

Note the absence of any UPDATE or DELETE route for approvals: FR-45 requires the
``approvals`` table to be append-only from the application's perspective, and
``tests/test_approvals_immutable.py`` asserts this router exposes none.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Response

from ..models import Phase
from ..phases import PHASE_ORDER, PhaseError, artifact_for
from ..schemas import (
    ApprovalCreate,
    ApprovalOut,
    ArtifactOut,
    RejectionCreate,
    RunOut,
    SessionDetail,
    SessionOut,
)
from ..services import workflow
from .deps import (
    CurrentSession,
    DbSession,
    phase_error_response,
    workflow_error_response,
)

router = APIRouter(tags=["sessions"])


def _detail(session: object, worktree: Path | None) -> SessionDetail:
    base = SessionOut.model_validate(session).model_dump()
    return SessionDetail(
        **base,
        artifacts=[ArtifactOut.model_validate(a) for a in session.artifacts],  # type: ignore[union-attr]
        approvals=[ApprovalOut.model_validate(a) for a in session.approvals],  # type: ignore[union-attr]
        runs=[RunOut.model_validate(r) for r in session.runs],  # type: ignore[union-attr]
        worktree_path=str(worktree) if worktree else None,
        available_phases=[p.value for p in PHASE_ORDER],
    )


@router.get("/sessions/{session_id}", response_model=SessionDetail)
async def get_session(db: DbSession, session: CurrentSession) -> SessionDetail:
    project = await workflow.get_project(db, session.project_id)
    if project is None:  # pragma: no cover - FK guarantees this
        raise HTTPException(status_code=404, detail="project not found")
    await workflow.advance_qa_if_srs_ready(db, project, session)
    return _detail(session, workflow.session_worktree(project, session))


@router.post("/sessions/{session_id}/approve", response_model=ApprovalOut)
async def approve(
    db: DbSession, session: CurrentSession, payload: ApprovalCreate
) -> ApprovalOut | Response:
    project = await workflow.get_project(db, session.project_id)
    if project is None:  # pragma: no cover - FK guarantees this
        raise HTTPException(status_code=404, detail="project not found")
    try:
        approval = await workflow.record_approval(
            db, project, session, payload.phase, notes=payload.notes
        )
    except PhaseError as exc:
        # AC-2: 422 {"error": "phase_not_ready", "required_approval": "srs"}
        return phase_error_response(exc)
    except workflow.WorkflowError as exc:
        return workflow_error_response(exc)
    return ApprovalOut.model_validate(approval)


@router.post("/sessions/{session_id}/reject", response_model=SessionOut)
async def reject(
    db: DbSession, session: CurrentSession, payload: RejectionCreate | None = None
) -> SessionOut:
    notes = payload.notes if payload else None
    await workflow.reject_current(db, session, notes=notes)
    return SessionOut.model_validate(session)


@router.get("/sessions/{session_id}/approvals", response_model=list[ApprovalOut])
async def list_approvals(session: CurrentSession) -> list[ApprovalOut]:
    return [ApprovalOut.model_validate(a) for a in session.approvals]


@router.get("/sessions/{session_id}/artifacts/{phase}")
async def read_artifact(
    db: DbSession, session: CurrentSession, phase: Phase
) -> dict[str, object]:
    """Return the raw text of a phase's artifact for the review panes."""
    project = await workflow.get_project(db, session.project_id)
    if project is None:  # pragma: no cover
        raise HTTPException(status_code=404, detail="project not found")

    relative = artifact_for(phase)
    if relative is None:
        raise HTTPException(status_code=404, detail="phase has no artifact")

    path = workflow.session_worktree(project, session) / relative
    if not path.exists():
        return {"phase": phase.value, "path": str(path), "exists": False, "content": ""}
    return {
        "phase": phase.value,
        "path": str(path),
        "exists": True,
        "content": path.read_text(encoding="utf-8", errors="replace"),
    }

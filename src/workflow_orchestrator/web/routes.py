"""Server-rendered UI routes.

These live under ``/ui`` so they cannot collide with the JSON API, whose paths
are fixed by SRS §4.3 and referenced directly by the acceptance criteria (AC-1
does ``GET /sessions/S1`` and expects JSON). ``/`` is the one exception — the API
defines no root route, so the project list is served there.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ..api.deps import AppSettings, CurrentProject, CurrentSession, DbSession
from ..models import RunStatus
from ..phases import PHASE_ARTIFACT, PHASE_ORDER, artifact_for
from ..services import workflow

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(include_in_schema=False)


def _unique_artifacts() -> list[tuple[str, str]]:
    """(phase, relative_path) pairs, one per distinct file."""
    seen: set[str] = set()
    pairs: list[tuple[str, str]] = []
    for phase, relative in PHASE_ARTIFACT.items():
        if relative in seen:
            continue
        seen.add(relative)
        pairs.append((phase.value, relative))
    return pairs


@router.get("/", response_class=HTMLResponse)
async def projects_page(request: Request, db: DbSession) -> HTMLResponse:
    projects = await workflow.list_projects(db)
    return templates.TemplateResponse(request, "projects.html", {"projects": projects})


@router.get("/ui/projects/{project_id}", response_class=HTMLResponse)
async def project_page(
    request: Request, db: DbSession, project: CurrentProject
) -> HTMLResponse:
    sessions = await workflow.list_sessions(db, project.id)
    return templates.TemplateResponse(
        request, "project_detail.html", {"project": project, "sessions": sessions}
    )


@router.get("/ui/sessions/{session_id}", response_class=HTMLResponse)
async def session_page(
    request: Request, db: DbSession, settings: AppSettings, session: CurrentSession
) -> HTMLResponse:
    project = await workflow.get_project(db, session.project_id)
    if project is None:  # pragma: no cover - FK guarantees this
        raise HTTPException(status_code=404, detail="project not found")

    await workflow.advance_qa_if_srs_ready(db, project, session)
    approved = {a.phase for a in session.approvals}

    # A run still in flight lets the page reattach its SSE stream on load.
    running_run = next(
        (r for r in session.runs if r.status == RunStatus.RUNNING.value), None
    )

    return templates.TemplateResponse(
        request,
        "session_detail.html",
        {
            "project": project,
            "session": session,
            "phase_order": [p.value for p in PHASE_ORDER],
            "approved_phases": approved,
            # QA and SRS share srs.md (FR-12 writes it, FR-14 approves it), so
            # dedupe by file to avoid rendering two identical buttons.
            "phase_artifacts": _unique_artifacts(),
            "current_artifact": artifact_for(session.phase),
            "worktree_path": str(workflow.session_worktree(project, session)),
            "running_run": running_run,
            "cost_ceiling": settings.WORKFLOW_RUN_COST_CEILING_USD,
            "timeout_minutes": settings.WORKFLOW_RUN_TIMEOUT_MINUTES,
            "merge_poll_seconds": settings.WORKFLOW_MERGE_POLL_SECONDS,
        },
    )


@router.get("/ui/wiki-queue", response_class=HTMLResponse)
async def wiki_queue_page(request: Request) -> HTMLResponse:
    """FR-40 — the wiki review queue page.

    Lives under ``/ui`` because SRS §4.3 assigns ``GET /wiki/review-queue`` to
    the JSON API, and the API router is included first.
    """
    return templates.TemplateResponse(request, "wiki_queue.html", {})

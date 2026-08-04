"""Project endpoints — SRS FR-1, FR-2, FR-3, §4.3."""

from __future__ import annotations

from fastapi import APIRouter, Response

from ..schemas import ProjectCreate, ProjectOut, SessionCreate, SessionOut
from ..services import workflow
from .deps import (
    AppSettings,
    CurrentProject,
    DbSession,
    workflow_error_response,
)

router = APIRouter(tags=["projects"])


@router.get("/projects", response_model=list[ProjectOut])
async def list_projects(db: DbSession, include_archived: bool = False) -> list[ProjectOut]:
    projects = await workflow.list_projects(db, include_archived=include_archived)
    return [ProjectOut.model_validate(p) for p in projects]


@router.post("/projects", response_model=ProjectOut, status_code=201)
async def create_project(
    db: DbSession, settings: AppSettings, payload: ProjectCreate
) -> ProjectOut | Response:
    try:
        project = await workflow.create_project(db, settings, payload)
    except workflow.WorkflowError as exc:
        return workflow_error_response(exc)
    return ProjectOut.model_validate(project)


@router.get("/projects/{project_id}", response_model=ProjectOut)
async def get_project(project: CurrentProject) -> ProjectOut:
    return ProjectOut.model_validate(project)


@router.post("/projects/{project_id}/archive", response_model=ProjectOut)
async def archive_project(db: DbSession, project: CurrentProject) -> ProjectOut:
    await workflow.archive_project(db, project)
    return ProjectOut.model_validate(project)


@router.get("/projects/{project_id}/sessions", response_model=list[SessionOut])
async def list_sessions(db: DbSession, project: CurrentProject) -> list[SessionOut]:
    sessions = await workflow.list_sessions(db, project.id)
    return [SessionOut.model_validate(s) for s in sessions]


@router.post("/projects/{project_id}/sessions", response_model=SessionOut, status_code=201)
async def create_session(
    db: DbSession,
    settings: AppSettings,
    project: CurrentProject,
    payload: SessionCreate,
) -> SessionOut | Response:
    try:
        session = await workflow.create_session(db, settings, project, payload)
    except workflow.WorkflowError as exc:
        # Keep workflow failures in the API's structured error shape.
        return workflow_error_response(exc)
    return SessionOut.model_validate(session)

"""Shared FastAPI dependencies and error translation."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Path as PathParam
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings, get_settings
from ..db import get_db
from ..models import Project, Session
from ..phases import PhaseError
from ..services import workflow

# scope="function": commit before the response is sent. FastAPI >= 0.118 runs
# the exit code of a yield dependency *after* the response by default, which
# would let a client see 200 for a write that is not yet durable (AC-1).
DbSession = Annotated[AsyncSession, Depends(get_db, scope="function")]
AppSettings = Annotated[Settings, Depends(get_settings)]


def phase_error_response(exc: PhaseError) -> JSONResponse:
    """SRS AC-2/AC-3 wire format: 422 with a machine-readable ``error`` key."""
    return JSONResponse(status_code=422, content=exc.as_payload())


def workflow_error_response(exc: workflow.WorkflowError) -> JSONResponse:
    return JSONResponse(status_code=exc.http_status, content=exc.as_payload())


async def get_project_or_404(
    db: DbSession, project_id: Annotated[str, PathParam()]
) -> Project:
    project = await workflow.get_project(db, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return project


async def get_session_or_404(
    db: DbSession, session_id: Annotated[str, PathParam()]
) -> Session:
    session = await workflow.get_session(db, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    return session


CurrentProject = Annotated[Project, Depends(get_project_or_404)]
CurrentSession = Annotated[Session, Depends(get_session_or_404)]

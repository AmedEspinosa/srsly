"""Run endpoints and SSE streaming — SRS FR-18..FR-22, §4.3."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, Request
from sse_starlette.sse import EventSourceResponse

from ..harness.base import HarnessOperation
from ..harness.prompts import implement_prompt
from ..logging import get_logger
from ..models import Harness, Phase, Run, RunStatus
from ..phases import PhaseError, check_can_enter
from ..runs.bus import get_bus
from ..runs.supervisor import NoRunnerAvailable, SupervisorError, get_supervisor
from ..schemas import RunOut
from ..services import workflow
from .deps import AppSettings, CurrentSession, DbSession, phase_error_response

log = get_logger(__name__)

router = APIRouter(tags=["runs"])

HEARTBEAT_SECONDS = 15


async def _project_or_404(db: DbSession, session: CurrentSession):
    project = await workflow.get_project(db, session.project_id)
    if project is None:  # pragma: no cover - FK guarantees this
        raise HTTPException(status_code=404, detail="project not found")
    return project


@router.post("/sessions/{session_id}/implement/run", response_model=RunOut)
async def start_implement_run(
    request: Request,
    db: DbSession,
    settings: AppSettings,
    session: CurrentSession,
) -> RunOut:
    """FR-18 — invoke the implementing harness against the approved plan."""
    project = await _project_or_404(db, session)

    try:
        check_can_enter(Phase.IMPLEMENT, session.approvals)
    except PhaseError as exc:
        return phase_error_response(exc)  # type: ignore[return-value]

    worktree = workflow.session_worktree(project, session)
    plan = worktree / ".workflow" / "plan.md"
    srs = worktree / ".workflow" / "srs.md"
    if not plan.exists():
        raise HTTPException(
            status_code=422,
            detail={"error": "artifact_missing", "artifact_path": str(plan)},
        )

    # FR-27 — the session branch is created at implement-phase start.
    from ..services.git import GitService, branch_name_for

    git = GitService(settings)
    branch = branch_name_for(session.id)
    await git.create_branch(worktree, branch)
    session.branch_name = branch

    # Commit before launching. The supervisor opens its own transaction to insert
    # the run row, and SQLite allows only one writer at a time — holding this
    # request's write lock across that call deadlocks with "database is locked".
    # WAL (NFR-5) removes reader/writer contention, not writer/writer.
    await db.commit()

    supervisor = get_supervisor(settings)
    try:
        run = await supervisor.start_run(
            session=session,
            worktree=worktree,
            phase=Phase.IMPLEMENT,
            harness=Harness(session.harness_implement),
            operation=HarnessOperation.IMPLEMENT,
            prompt=implement_prompt(plan, srs),
        )
    except NoRunnerAvailable as exc:
        raise HTTPException(
            status_code=503, detail={"error": "no_runner_available", "detail": str(exc)}
        )
    except SupervisorError as exc:
        raise HTTPException(
            status_code=502, detail={"error": "run_launch_failed", "detail": str(exc)}
        )
    return RunOut.model_validate(run)


@router.get("/sessions/{session_id}/runs", response_model=list[RunOut])
async def list_runs(session: CurrentSession) -> list[RunOut]:
    return [RunOut.model_validate(r) for r in session.runs]


@router.get("/sessions/{session_id}/runs/{run_id}", response_model=RunOut)
async def get_run(db: DbSession, session: CurrentSession, run_id: str) -> RunOut:
    run = await db.get(Run, run_id)
    if run is None or run.session_id != session.id:
        raise HTTPException(status_code=404, detail="run not found")
    return RunOut.model_validate(run)


@router.post("/sessions/{session_id}/runs/{run_id}/cancel", response_model=RunOut)
async def cancel_run(
    db: DbSession, settings: AppSettings, session: CurrentSession, run_id: str
) -> RunOut:
    run = await db.get(Run, run_id)
    if run is None or run.session_id != session.id:
        raise HTTPException(status_code=404, detail="run not found")
    await get_supervisor(settings).cancel(run_id)
    await db.refresh(run)
    return RunOut.model_validate(run)


@router.get("/sessions/{session_id}/runs/{run_id}/stream")
async def stream_run(
    request: Request,
    db: DbSession,
    settings: AppSettings,
    session: CurrentSession,
    run_id: str,
) -> EventSourceResponse:
    """FR-20 / NFR-8 — live run output as Server-Sent Events.

    The replay buffer is delivered first, so a client that connects late (or
    after a server restart replayed the persisted log) sees the whole run rather
    than joining mid-stream.
    """
    run = await db.get(Run, run_id)
    if run is None or run.session_id != session.id:
        raise HTTPException(status_code=404, detail="run not found")

    bus = get_bus()
    supervisor = get_supervisor(settings)

    async def publisher() -> AsyncIterator[dict[str, str]]:
        queue: asyncio.Queue[dict[str, str] | None] = asyncio.Queue()

        async def pump() -> None:
            try:
                async for event in bus.subscribe(run_id):
                    await queue.put(
                        {"event": "run", "data": json.dumps(event.to_dict())}
                    )
            finally:
                await queue.put(None)

        task = asyncio.create_task(pump())
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
                except TimeoutError:
                    # Keep the connection alive through long silent stretches.
                    yield {"event": "ping", "data": "{}"}
                    continue
                if item is None:
                    break
                yield item

            meter = supervisor.meter_for(run_id)
            yield {
                "event": "done",
                "data": json.dumps(
                    {
                        "run_id": run_id,
                        "tokens_used": meter.tokens_used if meter else None,
                        "cost_usd": meter.cost_usd if meter else None,
                    }
                ),
            }
        finally:
            task.cancel()

    return EventSourceResponse(publisher())


@router.get("/sessions/{session_id}/diff")
async def get_diff(
    db: DbSession, settings: AppSettings, session: CurrentSession
) -> dict[str, object]:
    """FR-22 — the diff the user approves before the review phase."""
    project = await _project_or_404(db, session)
    worktree = workflow.session_worktree(project, session)

    from ..services.git import GitService

    git = GitService(settings)
    diff = await git.diff(worktree)

    # Persist it so the implement phase has an approvable artifact on disk.
    target = worktree / ".workflow" / "diff.patch"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(diff, encoding="utf-8")
    await workflow.record_artifact(db, session, Phase.IMPLEMENT, target)

    return {"path": str(target), "diff": diff, "empty": not diff.strip()}

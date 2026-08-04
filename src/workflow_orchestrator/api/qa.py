"""QA phase endpoints — SRS FR-10..FR-13."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from sqlalchemy import delete

from ..models import Phase, QaTurn
from ..schemas import QaQuestion, QaState, QaSubmit, WikiContextStatus
from ..services import workflow
from ..services.bedrock import BedrockError
from ..services.requirements import QaState as EngineState
from ..services.requirements import RequirementsEngine, RequirementsError
from .deps import AppSettings, CurrentSession, DbSession

router = APIRouter(tags=["qa"])


def get_engine(request: Request, settings: AppSettings) -> RequirementsEngine:
    """Reuse the app-scoped engine so tests can inject a fake Bedrock client."""
    engine = getattr(request.app.state, "requirements_engine", None)
    if engine is None:
        engine = RequirementsEngine(settings)
        request.app.state.requirements_engine = engine
    return engine


def _to_schema(state: EngineState) -> QaState:
    return QaState(
        session_id=state.session_id,
        round=state.round,
        max_rounds=state.max_rounds,
        questions=[QaQuestion(**q.to_dict()) for q in state.questions],
        ready=state.ready,
        srs_written=state.srs_written,
        awaiting_first_prompt=state.awaiting_first_prompt,
    )


async def _project_for(db: DbSession, session: CurrentSession):
    project = await workflow.get_project(db, session.project_id)
    if project is None:  # pragma: no cover - FK guarantees this
        raise HTTPException(status_code=404, detail="project not found")
    return project


def _require_qa_phase(session: CurrentSession) -> None:
    if session.phase is not Phase.QA:
        raise HTTPException(
            status_code=422,
            detail={"error": "not_in_qa_phase", "current_phase": session.current_phase},
        )


@router.get("/sessions/{session_id}/qa", response_model=QaState)
async def get_qa_state(
    request: Request, db: DbSession, settings: AppSettings, session: CurrentSession
) -> QaState:
    project = await _project_for(db, session)
    engine = get_engine(request, settings)
    return _to_schema(await engine.state(db, project, session))


@router.get("/sessions/{session_id}/qa/context", response_model=WikiContextStatus)
async def get_qa_context(db: DbSession, session: CurrentSession) -> WikiContextStatus:
    """What wiki context this session has, or why it has none.

    Without this the QA phase fails soft and invisibly: a mis-levelled
    ``wiki_repo_path`` yields an empty context, and the only symptom is that the
    model asks generic questions.
    """
    project = await _project_for(db, session)
    from ..librarian.retrieval import inspect_context

    status = inspect_context(project)
    return WikiContextStatus(
        injected=bool(session.wiki_context_injected),
        available=status.has_context,
        initialized=status.initialized,
        wiki_repo_path=status.wiki_repo_path,
        resolved_wiki_dir=status.resolved_wiki_dir,
        super_summary_path=status.super_summary_path,
        super_summary_found=status.super_summary_found,
        super_summary_chars=status.super_summary_chars,
        pages=workflow.get_wiki_pages(session),
        hint=status.hint,
    )


async def _start(
    request: Request,
    db: DbSession,
    settings: AppSettings,
    project,
    session: CurrentSession,
) -> QaState:
    engine = get_engine(request, settings)

    # FR-35/FR-36 — inject wiki context before the first question round.
    from ..librarian.retrieval import build_session_context

    context, status = await build_session_context(db, settings, project, session)
    session.wiki_context_injected = status.has_context
    try:
        state = await engine.start(db, project, session, context=context)
    except BedrockError as exc:
        raise HTTPException(status_code=502, detail={"error": "bedrock_error", "detail": str(exc)})

    # The very first round can already be the last one — a model that opens with
    # ``ready: true`` finalizes inside start(), so the phase must advance here
    # too, not only on the answer path (FR-12).
    await workflow.advance_qa_if_srs_ready(db, project, session)
    return _to_schema(state)


@router.post("/sessions/{session_id}/qa/start", response_model=QaState)
async def start_qa(
    request: Request, db: DbSession, settings: AppSettings, session: CurrentSession
) -> QaState:
    _require_qa_phase(session)
    project = await _project_for(db, session)
    return await _start(request, db, settings, project, session)


@router.post("/sessions/{session_id}/qa/restart", response_model=QaState)
async def restart_qa(
    request: Request, db: DbSession, settings: AppSettings, session: CurrentSession
) -> QaState:
    """Discard the transcript and re-open the loop with fresh wiki context.

    Context is only injectable at round 0, so a session that opened without it
    cannot be repaired in place — a session started against a misconfigured
    wiki would otherwise be stuck with generic questions for its whole life.
    """
    _require_qa_phase(session)
    project = await _project_for(db, session)

    await db.execute(delete(QaTurn).where(QaTurn.session_id == session.id))
    await db.flush()
    return await _start(request, db, settings, project, session)


@router.post("/sessions/{session_id}/qa/answer", response_model=QaState)
async def submit_answers(
    request: Request,
    db: DbSession,
    settings: AppSettings,
    session: CurrentSession,
    payload: QaSubmit,
) -> QaState:
    _require_qa_phase(session)
    project = await _project_for(db, session)
    engine = get_engine(request, settings)

    answers: dict[str, object] = {a.question_id: a.answer for a in payload.answers}
    try:
        state = await engine.submit_answers(
            db, project, session, answers, end_early=payload.end_early
        )
    except RequirementsError as exc:
        raise HTTPException(status_code=422, detail={"error": "qa_error", "detail": str(exc)})
    except BedrockError as exc:
        raise HTTPException(status_code=502, detail={"error": "bedrock_error", "detail": str(exc)})

    # FR-12 -> the QA phase completes the moment srs.md exists.
    await workflow.advance_qa_if_srs_ready(db, project, session)
    return _to_schema(state)


@router.post("/sessions/{session_id}/qa/finalize", response_model=QaState)
async def finalize_qa(
    request: Request, db: DbSession, settings: AppSettings, session: CurrentSession
) -> QaState:
    """End the loop early and write the SRS now (FR-11)."""
    _require_qa_phase(session)
    project = await _project_for(db, session)
    engine = get_engine(request, settings)
    try:
        await engine.finalize(db, project, session)
    except (RequirementsError, BedrockError) as exc:
        raise HTTPException(status_code=502, detail={"error": "qa_error", "detail": str(exc)})

    await workflow.advance_qa_if_srs_ready(db, project, session)
    return _to_schema(await engine.state(db, project, session))

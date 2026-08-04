"""Requirements engine — SRS FR-10..FR-13.

Drives a structured clarifying-question loop against Bedrock, capped at
5 rounds x 5 questions (FR-10), refusing to emit an SRS until the loop ends
(FR-11), and writing ``.workflow/srs.md`` on completion (FR-12).

The transcript is persisted to ``qa_turns`` so a session in the middle of the
loop survives an application restart (FR-6).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..logging import get_logger
from ..models import Phase, Project, QaTurn, Session, utcnow
from ..phases import artifact_for
from .bedrock import BedrockClient, Message
from .clarify import (
    ClarifyPayload,
    Question,
    format_answers,
    parse_response,
    strip_clarify_block,
)
from .prompts import (
    FINALIZE_INSTRUCTION,
    MAX_QUESTIONS_PER_ROUND,
    MAX_ROUNDS,
    SRS_SYSTEM_PROMPT,
)
from .workflow import record_artifact, session_worktree

log = get_logger(__name__)

ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"


class RequirementsError(RuntimeError):
    pass


@dataclass
class QaState:
    """What the UI needs to render the current step of the loop."""

    session_id: str
    round: int
    max_rounds: int
    questions: list[Question]
    ready: bool
    srs_written: bool
    awaiting_first_prompt: bool


async def _load_turns(db: AsyncSession, session_id: str) -> list[QaTurn]:
    result = await db.execute(
        select(QaTurn).where(QaTurn.session_id == session_id).order_by(QaTurn.created_at)
    )
    return list(result.scalars().all())


async def _append_turn(
    db: AsyncSession, session_id: str, role: str, content: str, round_number: int
) -> QaTurn:
    turn = QaTurn(
        session_id=session_id,
        round_number=round_number,
        role=role,
        content=content,
        created_at=utcnow(),
    )
    db.add(turn)
    await db.flush()
    return turn


def _to_messages(turns: list[QaTurn]) -> list[Message]:
    return [Message(role=t.role, text=t.content) for t in turns]


def _rounds_used(turns: list[QaTurn]) -> int:
    """How many assistant question rounds have been issued so far."""
    return max((t.round_number for t in turns if t.role == ROLE_ASSISTANT), default=0)


def _latest_questions(turns: list[QaTurn]) -> tuple[list[Question], bool]:
    """Questions from the most recent assistant turn, and whether it said ready."""
    for turn in reversed(turns):
        if turn.role != ROLE_ASSISTANT:
            continue
        payload = parse_response(turn.content)
        if payload is None:
            return [], True  # no question block == the model is done asking
        return payload.questions, payload.ready
    return [], False


def srs_path(project: Project, session: Session) -> Path:
    return session_worktree(project, session) / (artifact_for(Phase.QA) or ".workflow/srs.md")


class RequirementsEngine:
    def __init__(self, settings: Settings, client: BedrockClient | None = None) -> None:
        self._settings = settings
        self._client = client or BedrockClient(settings)

    # --- state ---------------------------------------------------------------

    async def state(
        self, db: AsyncSession, project: Project, session: Session
    ) -> QaState:
        turns = await _load_turns(db, session.id)
        questions, ready = _latest_questions(turns)
        return QaState(
            session_id=session.id,
            round=_rounds_used(turns),
            max_rounds=MAX_ROUNDS,
            questions=questions,
            ready=ready,
            srs_written=srs_path(project, session).exists(),
            awaiting_first_prompt=not turns,
        )

    # --- loop ----------------------------------------------------------------

    async def start(
        self,
        db: AsyncSession,
        project: Project,
        session: Session,
        *,
        context: str = "",
    ) -> QaState:
        """Open the loop with the session's feature prompt (plus wiki context)."""
        turns = await _load_turns(db, session.id)
        if turns:
            return await self.state(db, project, session)

        opening = session.feature_prompt
        if context:
            opening = f"{context}\n\n---\n\n{opening}"

        await _append_turn(db, session.id, ROLE_USER, opening, round_number=0)
        return await self._ask_round(db, project, session)

    async def submit_answers(
        self,
        db: AsyncSession,
        project: Project,
        session: Session,
        answers: dict[str, object],
        *,
        end_early: bool = False,
    ) -> QaState:
        """Record answers and either ask another round or write the SRS."""
        turns = await _load_turns(db, session.id)
        if not turns:
            raise RequirementsError("question loop has not started yet")

        questions, _ready = _latest_questions(turns)
        rendered = format_answers(questions, answers) if questions else "(no answers)"
        await _append_turn(
            db, session.id, ROLE_USER, rendered, round_number=_rounds_used(turns)
        )

        if end_early:
            # FR-11 — the user may end the loop early; the SRS follows immediately.
            log.info("qa.ended_early", session_id=session.id)
            await self.finalize(db, project, session)
            return await self.state(db, project, session)

        return await self._ask_round(db, project, session)

    async def _ask_round(
        self, db: AsyncSession, project: Project, session: Session
    ) -> QaState:
        turns = await _load_turns(db, session.id)
        used = _rounds_used(turns)

        # FR-10 — hard cap at MAX_ROUNDS. Reaching it ends the loop and writes
        # the SRS rather than silently continuing to question the user.
        if used >= MAX_ROUNDS:
            log.info("qa.round_cap_reached", session_id=session.id, rounds=used)
            await self.finalize(db, project, session)
            return await self.state(db, project, session)

        result = await self._client.converse(
            _to_messages(turns), system=SRS_SYSTEM_PROMPT
        )
        payload = parse_response(result.text)

        await _append_turn(
            db, session.id, ROLE_ASSISTANT, result.text, round_number=used + 1
        )

        # No question block, or an explicit ready flag, means the model is done
        # asking. Either way the loop is over (FR-11) and the SRS can be written.
        if payload is None or payload.ready or not payload.questions:
            log.info(
                "qa.loop_complete",
                session_id=session.id,
                rounds=used + 1,
                reason="ready_flag" if payload and payload.ready else "no_questions",
            )
            await self.finalize(db, project, session)
            return await self.state(db, project, session)

        if len(payload.questions) > MAX_QUESTIONS_PER_ROUND:
            # Trim rather than reject: a model that over-asks should not stall
            # the session, and FR-10's cap is on what the user is shown.
            log.warning(
                "qa.questions_truncated",
                session_id=session.id,
                received=len(payload.questions),
                cap=MAX_QUESTIONS_PER_ROUND,
            )
            payload = ClarifyPayload(
                ready=payload.ready,
                questions=payload.questions[:MAX_QUESTIONS_PER_ROUND],
                version=payload.version,
            )

        log.info(
            "qa.round_issued",
            session_id=session.id,
            round=used + 1,
            questions=len(payload.questions),
        )
        return QaState(
            session_id=session.id,
            round=used + 1,
            max_rounds=MAX_ROUNDS,
            questions=payload.questions,
            ready=False,
            srs_written=srs_path(project, session).exists(),
            awaiting_first_prompt=False,
        )

    # --- finalization --------------------------------------------------------

    async def finalize(
        self, db: AsyncSession, project: Project, session: Session
    ) -> Path:
        """FR-12 — produce ``.workflow/srs.md`` in the session worktree."""
        turns = await _load_turns(db, session.id)
        messages = _to_messages(turns)
        messages.append(Message(role=ROLE_USER, text=FINALIZE_INSTRUCTION))

        result = await self._client.converse(
            messages, system=SRS_SYSTEM_PROMPT, max_tokens=16000
        )
        document = strip_clarify_block(result.text)
        if not document.strip():
            raise RequirementsError("the model returned an empty SRS document")

        target = srs_path(project, session)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(document + "\n", encoding="utf-8")

        await _append_turn(
            db,
            session.id,
            ROLE_ASSISTANT,
            document,
            round_number=_rounds_used(turns),
        )
        await record_artifact(db, session, Phase.QA, target)

        log.info(
            "qa.srs_written",
            session_id=session.id,
            path=str(target),
            characters=len(document),
        )
        return target

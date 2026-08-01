"""SQLAlchemy models — SRS §5.1.

All ``TEXT`` timestamps are ISO-8601 UTC (``2026-07-30T14:00:00Z``) per §5.2.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> str:
    """ISO-8601 UTC with a trailing Z, per SRS §5.2."""
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class Phase(str, enum.Enum):
    QA = "qa"
    SRS = "srs"
    PLAN = "plan"
    IMPLEMENT = "implement"
    REVIEW = "review"
    MERGE = "merge"
    COMPLETED = "completed"


class Harness(str, enum.Enum):
    CLAUDE_CODE = "claude_code"
    CODEX = "codex"

    def other(self) -> Harness:
        """The opposite harness — FR-23 cross-harness review."""
        return Harness.CODEX if self is Harness.CLAUDE_CODE else Harness.CLAUDE_CODE


class RunStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    COST_EXCEEDED = "cost_exceeded"

    @property
    def is_terminal(self) -> bool:
        return self is not RunStatus.PENDING and self is not RunStatus.RUNNING


class WikiOperation(str, enum.Enum):
    CREATE = "create"
    UPDATE = "update"


_PHASE_VALUES = tuple(p.value for p in Phase)
_HARNESS_VALUES = tuple(h.value for h in Harness)
_RUN_STATUS_VALUES = tuple(s.value for s in RunStatus)
_WIKI_OP_VALUES = tuple(o.value for o in WikiOperation)


def _in_clause(column: str, values: tuple[str, ...]) -> str:
    joined = ",".join(f"'{v}'" for v in values)
    return f"{column} IN ({joined})"


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String, nullable=False)
    repo_path: Mapped[str] = mapped_column(String, nullable=False)
    wiki_repo_path: Mapped[str] = mapped_column(String, nullable=False)
    wiki_super_summary_path: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=utcnow)
    archived_at: Mapped[str | None] = mapped_column(String, nullable=True)

    sessions: Mapped[list[Session]] = relationship(
        back_populates="project", cascade="all, delete-orphan", lazy="selectin"
    )


class Session(Base):
    __tablename__ = "sessions"
    __table_args__ = (
        CheckConstraint(_in_clause("current_phase", _PHASE_VALUES), name="ck_sessions_phase"),
        CheckConstraint(
            _in_clause("harness_implement", _HARNESS_VALUES), name="ck_sessions_harness_implement"
        ),
        CheckConstraint(
            _in_clause("harness_review", _HARNESS_VALUES), name="ck_sessions_harness_review"
        ),
        # FR-9 / AC-3: enforced at the DB level as well as the API layer.
        CheckConstraint("harness_implement != harness_review", name="ck_sessions_cross_harness"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    feature_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    current_phase: Mapped[str] = mapped_column(String, nullable=False, default=Phase.QA.value)
    harness_implement: Mapped[str] = mapped_column(String, nullable=False)
    harness_review: Mapped[str] = mapped_column(String, nullable=False)
    wiki_pages_injected: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=utcnow)
    completed_at: Mapped[str | None] = mapped_column(String, nullable=True)

    # Not in §5.1's DDL, but the merge phase needs somewhere to remember the PR
    # it opened so polling survives a restart (FR-29, NFR-4).
    branch_name: Mapped[str | None] = mapped_column(String, nullable=True)
    pr_url: Mapped[str | None] = mapped_column(String, nullable=True)
    pr_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pr_state: Mapped[str | None] = mapped_column(String, nullable=True)

    project: Mapped[Project] = relationship(back_populates="sessions", lazy="selectin")
    artifacts: Mapped[list[Artifact]] = relationship(
        back_populates="session", cascade="all, delete-orphan", lazy="selectin"
    )
    approvals: Mapped[list[Approval]] = relationship(
        back_populates="session", cascade="all, delete-orphan", lazy="selectin"
    )
    runs: Mapped[list[Run]] = relationship(
        back_populates="session", cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def phase(self) -> Phase:
        return Phase(self.current_phase)


class Artifact(Base):
    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), nullable=False)
    phase: Mapped[str] = mapped_column(String, nullable=False)
    file_path: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=utcnow)

    session: Mapped[Session] = relationship(back_populates="artifacts")


class Approval(Base):
    """Append-only. No UPDATE or DELETE route is exposed (FR-45, §5.2)."""

    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), nullable=False)
    phase: Mapped[str] = mapped_column(String, nullable=False)
    artifact_id: Mapped[str] = mapped_column(ForeignKey("artifacts.id"), nullable=False)
    approved_by: Mapped[str] = mapped_column(String, nullable=False, default="user")
    approved_at: Mapped[str] = mapped_column(String, nullable=False, default=utcnow)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    session: Mapped[Session] = relationship(back_populates="approvals")


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (
        CheckConstraint(_in_clause("status", _RUN_STATUS_VALUES), name="ck_runs_status"),
        CheckConstraint(_in_clause("harness", _HARNESS_VALUES), name="ck_runs_harness"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), nullable=False)
    phase: Mapped[str] = mapped_column(String, nullable=False)
    harness: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default=RunStatus.PENDING.value)
    container_id: Mapped[str | None] = mapped_column(String, nullable=True)
    log_path: Mapped[str | None] = mapped_column(String, nullable=True)
    started_at: Mapped[str | None] = mapped_column(String, nullable=True)
    ended_at: Mapped[str | None] = mapped_column(String, nullable=True)

    # Meter state (FR-21). Persisted so a reattached run resumes its accounting
    # rather than restarting from zero.
    tokens_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(nullable=False, default=0.0)

    session: Mapped[Session] = relationship(back_populates="runs")

    @property
    def run_status(self) -> RunStatus:
        return RunStatus(self.status)


class WikiWrite(Base):
    __tablename__ = "wiki_writes"
    __table_args__ = (
        CheckConstraint(_in_clause("operation", _WIKI_OP_VALUES), name="ck_wiki_writes_operation"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    session_id: Mapped[str | None] = mapped_column(ForeignKey("sessions.id"), nullable=True)
    page_path: Mapped[str] = mapped_column(String, nullable=False)
    operation: Mapped[str] = mapped_column(String, nullable=False)
    needs_review: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    index_token_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    written_at: Mapped[str] = mapped_column(String, nullable=False, default=utcnow)


class SuperSummaryProposal(Base):
    """FR-41 / AC-8 — regeneration is proposed, never executed without approval."""

    __tablename__ = "super_summary_proposals"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    session_id: Mapped[str | None] = mapped_column(ForeignKey("sessions.id"), nullable=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    super_summary_id: Mapped[str] = mapped_column(String, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=utcnow)
    resolved_at: Mapped[str | None] = mapped_column(String, nullable=True)


class QaTurn(Base):
    """Persisted Q&A transcript so the QA phase survives a restart (FR-6)."""

    __tablename__ = "qa_turns"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), nullable=False)
    round_number: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String, nullable=False)  # user | assistant
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=utcnow)

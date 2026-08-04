"""Core session/approval operations shared by the JSON API and the HTML UI."""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..logging import get_logger
from ..models import (
    Approval,
    Artifact,
    Harness,
    Phase,
    Project,
    Session,
    utcnow,
)
from ..phases import (
    PhaseError,
    artifact_for,
    check_can_approve,
    next_phase,
    previous_phase,
)
from ..schemas import ProjectCreate, SessionCreate
from .git import GitService, worktree_path

log = get_logger(__name__)


class WorkflowError(Exception):
    error_code = "workflow_error"
    http_status = 422

    def as_payload(self) -> dict[str, object]:
        return {"error": self.error_code, "detail": str(self)}


class ArtifactMissing(WorkflowError):
    error_code = "artifact_missing"

    def __init__(self, path: str) -> None:
        super().__init__(path)
        self.path = path

    def __str__(self) -> str:  # pragma: no cover - message only
        return f"expected artifact {self.path} does not exist yet"

    def as_payload(self) -> dict[str, object]:
        return {"error": self.error_code, "artifact_path": self.path}


class NotAGitRepo(WorkflowError):
    error_code = "not_a_git_repo"
    http_status = 400

    def __init__(self, path: str) -> None:
        super().__init__(path)
        self.path = path

    def __str__(self) -> str:  # pragma: no cover - message only
        return f"{self.path} is not a git repository"

    def as_payload(self) -> dict[str, object]:
        return {"error": self.error_code, "repo_path": self.path}


# --- projects -----------------------------------------------------------------


async def create_project(
    db: AsyncSession, settings: Settings, payload: ProjectCreate
) -> Project:
    git = GitService(settings)
    repo_path = str(Path(payload.repo_path).expanduser())
    if not await git.is_git_repo(repo_path):
        raise NotAGitRepo(repo_path)

    project = Project(
        name=payload.name,
        repo_path=repo_path,
        wiki_repo_path=str(Path(payload.wiki_repo_path).expanduser()),
        wiki_super_summary_path=payload.wiki_super_summary_path,
    )
    db.add(project)
    await db.flush()
    await git.ensure_scratch_excluded(repo_path)
    log.info("project.created", project_id=project.id, name=project.name)
    return project


async def list_projects(db: AsyncSession, *, include_archived: bool = False) -> list[Project]:
    stmt = select(Project).order_by(Project.created_at.desc())
    if not include_archived:
        stmt = stmt.where(Project.archived_at.is_(None))
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def get_project(db: AsyncSession, project_id: str) -> Project | None:
    return await db.get(Project, project_id)


async def archive_project(db: AsyncSession, project: Project) -> Project:
    project.archived_at = utcnow()
    log.info("project.archived", project_id=project.id)
    return project


# --- sessions -----------------------------------------------------------------


async def create_session(
    db: AsyncSession, settings: Settings, project: Project, payload: SessionCreate
) -> Session:
    session = Session(
        project_id=project.id,
        feature_prompt=payload.feature_prompt,
        current_phase=Phase.QA.value,
        harness_implement=payload.harness_implement.value,
        harness_review=payload.harness_review.value,
        wiki_pages_injected="[]",
    )
    db.add(session)
    await db.flush()

    git = GitService(settings)
    await git.create_worktree(project.repo_path, session.id)

    log.info(
        "session.created",
        session_id=session.id,
        project_id=project.id,
        harness_implement=session.harness_implement,
        harness_review=session.harness_review,
    )
    return session


async def list_sessions(db: AsyncSession, project_id: str) -> list[Session]:
    result = await db.execute(
        select(Session)
        .where(Session.project_id == project_id)
        .order_by(Session.created_at.desc())
    )
    return list(result.scalars().all())


async def get_session(db: AsyncSession, session_id: str) -> Session | None:
    return await db.get(Session, session_id)


def session_worktree(project: Project, session: Session) -> Path:
    return worktree_path(project.repo_path, session.id)


def set_wiki_pages(session: Session, pages: list[str]) -> None:
    session.wiki_pages_injected = json.dumps(pages)


def get_wiki_pages(session: Session) -> list[str]:
    try:
        parsed = json.loads(session.wiki_pages_injected or "[]")
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


# --- artifacts & approvals ----------------------------------------------------


async def record_artifact(
    db: AsyncSession, session: Session, phase: Phase, file_path: Path | str
) -> Artifact:
    """Register an artifact, reusing the row if this phase already has one."""
    path = str(file_path)
    result = await db.execute(
        select(Artifact).where(
            Artifact.session_id == session.id,
            Artifact.phase == phase.value,
            Artifact.file_path == path,
        )
    )
    existing = result.scalar_one_or_none()
    if existing is not None:
        return existing

    artifact = Artifact(session_id=session.id, phase=phase.value, file_path=path)
    db.add(artifact)
    await db.flush()
    log.info(
        "artifact.recorded", session_id=session.id, phase=phase.value, file_path=path
    )
    return artifact


async def _artifact_for_approval(
    db: AsyncSession, project: Project, session: Session, phase: Phase
) -> Artifact:
    relative = artifact_for(phase)
    if relative is None:
        raise ArtifactMissing(f"<no artifact defined for phase {phase.value}>")

    absolute = session_worktree(project, session) / relative
    if not absolute.exists():
        raise ArtifactMissing(str(absolute))
    return await record_artifact(db, session, phase, absolute)


async def advance_qa_if_srs_ready(
    db: AsyncSession, project: Project, session: Session
) -> bool:
    """Move ``qa -> srs`` once ``srs.md`` exists.

    FR-5 makes ``qa`` the entry point, so it carries no approval of its own; FR-12
    says the QA loop's completion *is* writing ``srs.md``, and FR-14 puts the
    approval of that file under the ``srs`` phase. Rather than leaving the
    transition implicit inside the approval gate, it is spelled out here and
    called from every path that observes session state.

    Returns True if the session moved.
    """
    if session.phase is not Phase.QA:
        return False

    srs_path = session_worktree(project, session) / (artifact_for(Phase.QA) or "")
    if not srs_path.exists():
        return False

    await record_artifact(db, session, Phase.QA, srs_path)
    session.current_phase = Phase.SRS.value
    await db.flush()
    log.info("phase.qa_completed", session_id=session.id, artifact=str(srs_path))
    return True


async def record_approval(
    db: AsyncSession,
    project: Project,
    session: Session,
    phase: Phase,
    *,
    notes: str | None = None,
) -> Approval:
    """FR-44/FR-45 — append-only, timestamped, immutable once recorded.

    Raises :class:`~workflow_orchestrator.phases.PhaseError` subclasses when the
    transition is not permitted; the API layer maps those to 422.
    """
    await advance_qa_if_srs_ready(db, project, session)
    check_can_approve(session.phase, phase, session.approvals)

    artifact = await _artifact_for_approval(db, project, session, phase)

    approval = Approval(
        session_id=session.id,
        phase=phase.value,
        artifact_id=artifact.id,
        approved_by="user",
        approved_at=utcnow(),
        notes=notes,
    )
    db.add(approval)
    session.approvals.append(approval)

    advanced = next_phase(phase)
    session.current_phase = advanced.value
    if advanced is Phase.COMPLETED:
        session.completed_at = utcnow()

    await db.flush()
    log.info(
        "approval.recorded",
        session_id=session.id,
        phase=phase.value,
        advanced_to=advanced.value,
        approved_at=approval.approved_at,
    )
    return approval


async def reject_current(
    db: AsyncSession, session: Session, *, notes: str | None = None
) -> Session:
    """FR-13 — rejection returns the session to the preceding phase."""
    current = session.phase
    target = previous_phase(current)
    session.current_phase = target.value
    await db.flush()
    log.info(
        "phase.rejected",
        session_id=session.id,
        from_phase=current.value,
        to_phase=target.value,
        notes=notes,
    )
    return session


__all__ = [
    "ArtifactMissing",
    "NotAGitRepo",
    "PhaseError",
    "WorkflowError",
    "advance_qa_if_srs_ready",
    "archive_project",
    "create_project",
    "create_session",
    "get_project",
    "get_session",
    "get_wiki_pages",
    "list_projects",
    "list_sessions",
    "record_approval",
    "record_artifact",
    "reject_current",
    "session_worktree",
    "set_wiki_pages",
    "Harness",
]

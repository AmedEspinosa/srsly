"""Merge phase endpoints — SRS FR-27..FR-30, OQ-3."""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException

from ..config import Settings
from ..db import session_scope
from ..logging import get_logger
from ..models import Phase, Session, utcnow
from ..phases import PhaseError, check_can_enter
from ..schemas import SessionOut
from ..services import workflow
from ..services.git import GitService, branch_name_for, pr_title_from_prompt
from .deps import AppSettings, CurrentSession, DbSession, phase_error_response

log = get_logger(__name__)

router = APIRouter(tags=["merge"])

MERGED_STATES = {"MERGED"}
CLOSED_STATES = {"CLOSED"}

PR_BODY_TEMPLATE = """\
{summary}

Produced by the AI-driven development workflow orchestrator.

| Artifact | Path |
| --- | --- |
| Specification | `{srs}` |
| Plan | `{plan}` |
| Review | `{review}` |

- Implemented by: **{harness_implement}**
- Reviewed by: **{harness_review}**

Session `{session_id}`.
"""


def build_pr_body(session: Session) -> str:
    """FR-28 — body links srs.md, plan.md and review.md."""
    return PR_BODY_TEMPLATE.format(
        summary=session.feature_prompt.strip(),
        srs=".workflow/srs.md",
        plan=".workflow/plan.md",
        review=".workflow/review.md",
        harness_implement=session.harness_implement,
        harness_review=session.harness_review,
        session_id=session.id,
    )


@router.post("/sessions/{session_id}/merge/open-pr")
async def open_pull_request(
    db: DbSession, settings: AppSettings, session: CurrentSession
) -> dict[str, object]:
    """FR-27/FR-28 — push the session branch and open a PR."""
    project = await workflow.get_project(db, session.project_id)
    if project is None:  # pragma: no cover
        raise HTTPException(status_code=404, detail="project not found")

    try:
        check_can_enter(Phase.MERGE, session.approvals)
    except PhaseError as exc:
        return phase_error_response(exc)  # type: ignore[return-value]

    worktree = workflow.session_worktree(project, session)
    git = GitService(settings)

    branch = session.branch_name or branch_name_for(session.id)
    await git.create_branch(worktree, branch)
    session.branch_name = branch

    committed = await git.commit_all(worktree, pr_title_from_prompt(session.feature_prompt))
    log.info("merge.committed", session_id=session.id, made_commit=committed)

    if not await git.has_remote(worktree):
        raise HTTPException(
            status_code=422,
            detail={
                "error": "no_remote",
                "detail": "the repository has no 'origin' remote to push to",
            },
        )

    try:
        await git.push_branch(worktree, branch)
        pr = await git.create_pull_request(
            worktree,
            title=pr_title_from_prompt(session.feature_prompt),
            body=build_pr_body(session),
        )
    except Exception as exc:
        log.error("merge.pr_failed", session_id=session.id, error=str(exc))
        raise HTTPException(
            status_code=502, detail={"error": "pr_failed", "detail": str(exc)}
        )

    session.pr_url = pr.url
    session.pr_number = pr.number
    session.pr_state = pr.state
    await db.flush()

    # Record the PR as the merge phase's approvable artifact.
    pr_file = worktree / ".workflow" / "pr.json"
    pr_file.parent.mkdir(parents=True, exist_ok=True)
    pr_file.write_text(
        json.dumps({"number": pr.number, "url": pr.url, "state": pr.state}, indent=2),
        encoding="utf-8",
    )
    await workflow.record_artifact(db, session, Phase.MERGE, pr_file)

    log.info("merge.pr_opened", session_id=session.id, url=pr.url, number=pr.number)
    return {"url": pr.url, "number": pr.number, "state": pr.state, "branch": branch}


@router.get("/sessions/{session_id}/merge/status")
async def merge_status(
    db: DbSession, settings: AppSettings, session: CurrentSession
) -> dict[str, object]:
    """FR-29 — poll ``gh pr view --json state`` and complete on merge."""
    project = await workflow.get_project(db, session.project_id)
    if project is None:  # pragma: no cover
        raise HTTPException(status_code=404, detail="project not found")

    worktree = workflow.session_worktree(project, session)
    pr = await GitService(settings).view_pull_request(worktree)
    if pr is None:
        return {"state": session.pr_state or "UNKNOWN", "url": session.pr_url}

    session.pr_state = pr.state
    session.pr_url = pr.url or session.pr_url
    session.pr_number = pr.number or session.pr_number

    merged = pr.state.upper() in MERGED_STATES
    if merged:
        await _on_merged(db, settings, session)
    await db.flush()

    return {
        "state": pr.state,
        "url": pr.url,
        "number": pr.number,
        "merged": merged,
        "session_phase": session.current_phase,
    }


async def _on_merged(db: DbSession, settings: Settings, session: Session) -> None:
    """FR-29/FR-30 — trigger the Librarian and complete the session."""
    if session.current_phase == Phase.COMPLETED.value:
        return

    session.current_phase = Phase.COMPLETED.value
    session.completed_at = utcnow()
    await db.flush()
    log.info("merge.session_completed", session_id=session.id)

    # FR-29 — post-merge wiki write-back. Enqueued, not awaited: the merge
    # response must not block on a wiki agent run.
    from ..librarian.writeback import enqueue_post_merge

    await enqueue_post_merge(settings, session.project_id, session.id)


class MergePoller:
    """Background poller — OQ-3, resolved to polling at 60s intervals."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        from sqlalchemy import select

        interval = self._settings.WORKFLOW_MERGE_POLL_SECONDS
        git = GitService(self._settings)

        while True:
            try:
                await asyncio.sleep(interval)
                async with session_scope() as db:
                    rows = (
                        await db.execute(
                            select(Session).where(
                                Session.pr_url.is_not(None),
                                Session.completed_at.is_(None),
                            )
                        )
                    ).scalars().all()

                    for session in rows:
                        project = await workflow.get_project(db, session.project_id)
                        if project is None:  # pragma: no cover
                            continue
                        worktree = workflow.session_worktree(project, session)
                        pr = await git.view_pull_request(worktree)
                        if pr is None:
                            continue
                        session.pr_state = pr.state
                        if pr.state.upper() in MERGED_STATES:
                            await _on_merged(db, self._settings, session)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - defensive
                log.error("merge.poll_failed", error=str(exc))

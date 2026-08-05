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

- Implemented by: **{harness_implement}**
- Reviewed by: **{harness_review}**

The specification, plan and review for this change are session artifacts held \
outside the repository, under `worktrees/{session_id}/`. An as-built record \
reconciling the specification against what actually shipped is produced after \
this pull request merges.

Session `{session_id}`.
"""


def build_pr_body(session: Session) -> str:
    """FR-28 — attribute the change and point at where its artifacts live.

    The body used to carry a table of `.workflow/` paths. Those artifacts are no
    longer committed (SRS §4.5 — session scratch does not belong in a pull
    request), so the table linked three paths that did not exist. Naming the
    worktree is honest; a dead link is not.
    """
    return PR_BODY_TEMPLATE.format(
        summary=session.feature_prompt.strip(),
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


@router.get("/sessions/{session_id}/as-built")
async def read_as_built(db: DbSession, session: CurrentSession) -> dict[str, object]:
    """The post-merge record: what shipped, and where it left the SRS behind.

    Written by the write-back, so it only exists once the pull request has
    merged. Absent is a normal state, not an error.
    """
    from ..services import as_built as as_built_service

    project = await workflow.get_project(db, session.project_id)
    if project is None:  # pragma: no cover
        raise HTTPException(status_code=404, detail="project not found")

    worktree = workflow.session_worktree(project, session)
    document = as_built_service.read(worktree)
    if document is None:
        return {"exists": False, "requirements": [], "content": "", "counts": {}}

    return {
        "exists": True,
        "content": document.prose,
        "requirements": [r.to_dict() for r in document.requirements],
        "counts": document.counts(),
        "gaps": len(document.gaps),
    }


@router.post("/sessions/{session_id}/wiki-writeback")
async def trigger_wiki_writeback(
    db: DbSession, settings: AppSettings, session: CurrentSession, force: bool = False
) -> dict[str, object]:
    """Run the post-merge write-back for one session, on demand.

    The automatic path needs ``gh`` to observe the merge. When it cannot — the
    pull request merged unobserved, ``gh`` is not installed, an earlier
    write-back failed — this is the way back in. ``force`` ignores the claim and
    re-runs an ingest that has already happened, so it is opt-in.
    """
    from ..librarian.writeback import enqueue_post_merge, release_in_flight

    if session.wiki_writeback_at is not None and not force:
        return {
            "session_id": session.id,
            "enqueued": False,
            "wiki_writeback_at": session.wiki_writeback_at,
            "reason": "already written back — pass force=true to run it again",
        }

    if force:
        session.wiki_writeback_at = None
        await db.flush()
        release_in_flight(session.id)

    await enqueue_post_merge(settings, session.project_id, session.id)
    log.info("wiki.writeback_triggered", session_id=session.id, force=force)
    return {
        "session_id": session.id,
        "enqueued": True,
        "wiki_writeback_at": None,
        "reason": "queued",
    }


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
    """FR-29/FR-30 — complete the session and trigger the Librarian.

    The guard is the write-back claim, not the phase. Those are different facts:
    a session completed by hand is finished, but the wiki has not ingested it,
    and gating ingestion on "finished" made the manual path a permanent lockout
    — the poller skipped the session for being complete, and so did this
    function.
    """
    if session.wiki_writeback_at is not None:
        return

    # Only on the merge-detection path. A session already completed by hand
    # keeps its original approval timestamp rather than having it overwritten
    # with the moment we happened to notice the merge.
    if session.current_phase != Phase.COMPLETED.value:
        session.current_phase = Phase.COMPLETED.value
        session.completed_at = utcnow()
        await db.flush()
        log.info("merge.session_completed", session_id=session.id)

    # FR-29 — post-merge wiki write-back. Enqueued, not awaited: the merge
    # response must not block on a wiki agent run.
    from ..librarian.writeback import enqueue_post_merge

    await enqueue_post_merge(settings, session.project_id, session.id)


async def reconcile_after_manual_approval(
    db: DbSession, settings: Settings, session: Session
) -> bool:
    """Fire the write-back if the user approved a pull request that has merged.

    Approving the merge phase by hand is a first-class path — the approve button
    is present in every phase — but it is not proof the pull request landed. So
    check once, and act only on MERGED.

    Deliberately *not* unconditional. ``generate_as_built`` runs ``gh pr diff``,
    and ``AS_BUILT_FRAMING`` tells the Librarian that document overrides
    existing wiki pages; producing one for an open pull request would record a
    pre-merge diff as durable truth. On anything other than MERGED this does
    nothing and the poller picks the session up when it really merges.
    """
    if session.wiki_writeback_at is not None or not session.pr_url:
        return False

    project = await workflow.get_project(db, session.project_id)
    if project is None:  # pragma: no cover - FK guarantees this
        return False

    worktree = workflow.session_worktree(project, session)
    pr = await GitService(settings).view_pull_request(worktree)
    if pr is None or pr.state.upper() not in MERGED_STATES:
        log.info(
            "merge.approved_without_confirmed_merge",
            session_id=session.id,
            pr_state=pr.state if pr else None,
        )
        return False

    session.pr_state = pr.state
    await _on_merged(db, settings, session)
    return True


async def poll_open_pull_requests(settings: Settings) -> list[str]:
    """One poll pass. Returns the ids of sessions newly detected as merged.

    Extracted from the poller loop so the predicate is testable without waiting
    out a poll interval — the predicate is the thing that was wrong.
    """
    from sqlalchemy import select

    git = GitService(settings)
    merged: list[str] = []

    async with session_scope() as db:
        rows = (
            await db.execute(
                select(Session).where(
                    Session.pr_url.is_not(None),
                    # Not ``completed_at``: a session completed by hand still
                    # needs its write-back, and gating on completion is what
                    # made that unreachable.
                    Session.wiki_writeback_at.is_(None),
                )
            )
        ).scalars().all()

        for session in rows:
            # One unreachable worktree or a missing ``gh`` must not abort the
            # sessions behind it in the result set — that would silently stop
            # polling everything after the first bad row.
            try:
                project = await workflow.get_project(db, session.project_id)
                if project is None:  # pragma: no cover
                    continue
                worktree = workflow.session_worktree(project, session)
                pr = await git.view_pull_request(worktree)
                if pr is None:
                    continue
                session.pr_state = pr.state
                if pr.state.upper() in MERGED_STATES:
                    await _on_merged(db, settings, session)
                    merged.append(session.id)
            except Exception as exc:
                log.error(
                    "merge.poll_session_failed", session_id=session.id, error=str(exc)
                )

    return merged


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
        interval = self._settings.WORKFLOW_MERGE_POLL_SECONDS
        while True:
            try:
                await asyncio.sleep(interval)
                await poll_open_pull_requests(self._settings)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - defensive
                log.error("merge.poll_failed", error=str(exc))

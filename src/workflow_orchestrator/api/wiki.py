"""Wiki review queue endpoints — SRS FR-40, FR-41, FR-43, §4.3."""

from __future__ import annotations

import difflib
from pathlib import Path

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from ..librarian.layout import WikiLayout, layout_for
from ..librarian.lint import run_lint
from ..librarian.queue import get_queue
from ..librarian.writeback import (
    clear_needs_review,
    has_needs_review,
    log_index_metrics,
)
from ..logging import get_logger
from ..models import Project, SuperSummaryProposal, WikiWrite, utcnow
from .deps import AppSettings, DbSession

log = get_logger(__name__)

router = APIRouter(tags=["wiki"])


async def _candidate_layouts(db: DbSession) -> list[WikiLayout]:
    """Every distinct wiki repo any project is configured against.

    Deduped by ``repo_root``, not by project: two projects can share one vault
    (and in practice do), and ``discover_repo_root`` normalises ``…/llm-wiki``
    and ``…/llm-wiki/wiki`` to the same root. "Which project owns this page" was
    never the question — which repo holds it is.
    """
    projects = (await db.execute(select(Project))).scalars().all()
    seen: set[str] = set()
    layouts: list[WikiLayout] = []
    for project in projects:
        layout = layout_for(project.wiki_repo_path)
        key = str(layout.repo_root)
        if key not in seen:
            seen.add(key)
            layouts.append(layout)
    return layouts


async def _locate_page(db: DbSession, page_path: str) -> tuple[WikiLayout, Path]:
    """The layout and on-disk path for a queued page.

    Resolution goes through ``layout.resolve``, which knows both vault shapes —
    the previous lookup joined the *configured* path with a path stored relative
    to the discovered repo root, so it never matched and always fell through to
    the first project. There is no such fallback now: acting on the wrong repo
    is worse than refusing, because rejecting a page deletes files.
    """
    layouts = await _candidate_layouts(db)
    if not layouts:
        raise HTTPException(status_code=404, detail="no project configured")

    escaped = 0
    for layout in layouts:
        try:
            path = layout.resolve(page_path)
        except ValueError:
            escaped += 1
            continue
        if path.exists():
            return layout, path

    if escaped == len(layouts):
        raise HTTPException(status_code=400, detail="path escapes the wiki repo")
    raise HTTPException(status_code=404, detail="page not found")


async def _locate_for_reject(db: DbSession, page_path: str) -> tuple[WikiLayout, Path]:
    """Like :func:`_locate_page`, but tolerates a page that is already gone.

    Rejecting a page that no longer exists on disk is legitimate — it may have
    been deleted by hand, or by an earlier reject — and must still clear the
    queue entry. The path is still validated against every candidate repo; what
    is relaxed is only the existence check.
    """
    try:
        return await _locate_page(db, page_path)
    except HTTPException as exc:
        if exc.status_code != 404 or exc.detail != "page not found":
            raise

    layouts = await _candidate_layouts(db)
    resolvable = []
    for layout in layouts:
        try:
            resolvable.append((layout, layout.resolve(page_path)))
        except ValueError:  # pragma: no cover - _locate_page ruled this out
            continue
    if len(resolvable) == 1:
        return resolvable[0]

    # Several vaults could hold this path and none does. Let git decide: the
    # first repo that can restore the file is the one that owned it.
    from ..services.process import run_command

    for layout, path in resolvable:
        probe = await run_command(
            ["git", "cat-file", "-e", f"HEAD:{_git_path(layout, path)}"],
            cwd=layout.repo_root,
            timeout=30,
        )
        if probe.ok:
            return layout, path
    raise HTTPException(status_code=404, detail="page not found")


def _git_path(layout: WikiLayout, path: Path) -> str:
    """Repo-relative POSIX path, after ``resolve`` may have redirected it.

    Git must be handed the path the file actually has. Passing the stored string
    straight through makes ``git show HEAD:…`` miss whenever the vault nests its
    page directories, which reports every such page as newly created.
    """
    return path.relative_to(layout.repo_root.resolve()).as_posix()


@router.get("/wiki/review-queue")
async def review_queue(db: DbSession) -> dict[str, object]:
    """FR-40 — every page still flagged ``needs_review: true``."""
    rows = (
        await db.execute(
            select(WikiWrite)
            .where(WikiWrite.needs_review == 1)
            .order_by(WikiWrite.written_at.desc())
        )
    ).scalars().all()

    # Collapse repeated writes to the same page; the newest is what to review.
    seen: set[str] = set()
    items: list[dict[str, object]] = []
    for row in rows:
        if row.page_path in seen:
            continue
        seen.add(row.page_path)
        items.append(
            {
                "page_path": row.page_path,
                "operation": row.operation,
                "written_at": row.written_at,
                "session_id": row.session_id,
                "index_token_count": row.index_token_count,
            }
        )

    proposals = (
        await db.execute(
            select(SuperSummaryProposal)
            .where(SuperSummaryProposal.status == "pending")
            .order_by(SuperSummaryProposal.created_at.desc())
        )
    ).scalars().all()

    return {
        "items": items,
        "queue_depth": get_queue().depth,
        # FR-41 / AC-8 — regeneration is proposed, never executed.
        "super_summary_proposals": [
            {
                "id": p.id,
                "project_id": p.project_id,
                "super_summary_id": p.super_summary_id,
                "rationale": p.rationale,
                "created_at": p.created_at,
            }
            for p in proposals
        ],
    }


@router.get("/wiki/review-queue/{page_path:path}/diff")
async def page_diff(db: DbSession, page_path: str) -> dict[str, object]:
    """FR-40 — inline diff of an automated write against its committed version."""
    layout, path = await _locate_page(db, page_path)
    git_path = _git_path(layout, path)

    current = path.read_text(encoding="utf-8", errors="replace")

    # Compare against the last committed version, which is what "before the
    # automated write" means for a git-backed wiki.
    from ..services.process import run_command

    previous = ""
    result = await run_command(
        ["git", "show", f"HEAD:{git_path}"], cwd=layout.repo_root, timeout=30
    )
    if result.ok:
        previous = result.stdout

    diff = "".join(
        difflib.unified_diff(
            previous.splitlines(keepends=True),
            current.splitlines(keepends=True),
            fromfile=f"a/{git_path}",
            tofile=f"b/{git_path}",
        )
    )
    return {
        "page_path": page_path,
        "diff": diff or "(no differences against HEAD)",
        "content": current,
        "needs_review": has_needs_review(current),
        "is_new": not result.ok,
    }


@router.post("/wiki/review-queue/{page_path:path}/approve")
async def approve_page(db: DbSession, page_path: str) -> dict[str, object]:
    """FR-40 — clear the ``needs_review`` flag."""
    _layout, path = await _locate_page(db, page_path)

    # A page with no frontmatter cannot carry the flag, so there is nothing to
    # clear. Report that rather than writing a frontmatter block onto a page
    # nobody flagged — and say so in the response, so the UI cannot present a
    # no-op as a successful edit.
    text = path.read_text(encoding="utf-8", errors="replace")
    cleared = clear_needs_review(text)
    if cleared is None:
        log.info("wiki.needs_review_absent", page_path=page_path)
    elif cleared != text:
        path.write_text(cleared, encoding="utf-8")

    rows = (
        await db.execute(select(WikiWrite).where(WikiWrite.page_path == page_path))
    ).scalars().all()
    for row in rows:
        row.needs_review = 0

    log.info("wiki.page_approved", page_path=page_path)
    return {
        "page_path": page_path,
        "needs_review": False,
        "frontmatter_cleared": cleared is not None,
    }


@router.post("/wiki/review-queue/{page_path:path}/reject")
async def reject_page(db: DbSession, page_path: str) -> dict[str, object]:
    """Revert an automated write to its committed state.

    Note this does *not* leave the wiki repo clean. Pages are committed carrying
    ``needs_review: true``, so reverting restores that flag — which is how the
    vault ended up full of pages the file called unreviewed and the database
    called dispositioned. Rejecting is a review, so the flag is cleared
    afterwards, at the cost of a one-line working-tree diff.
    """
    from ..services.process import run_command

    # Validate before acting. The destructive branch below is an unlink, and it
    # used to run on a path that had never been checked against the repo root.
    layout, path = await _locate_for_reject(db, page_path)

    result = await run_command(
        ["git", "checkout", "--", _git_path(layout, path)],
        cwd=layout.repo_root,
        timeout=30,
    )
    reverted = result.ok
    frontmatter_cleared = False
    if reverted:
        if path.exists():
            text = path.read_text(encoding="utf-8", errors="replace")
            cleared = clear_needs_review(text)
            if cleared is not None and cleared != text:
                path.write_text(cleared, encoding="utf-8")
                frontmatter_cleared = True
    else:
        # The page was newly created, so there is nothing to revert to.
        path.unlink(missing_ok=True)

    rows = (
        await db.execute(select(WikiWrite).where(WikiWrite.page_path == page_path))
    ).scalars().all()
    for row in rows:
        row.needs_review = 0

    log.info(
        "wiki.page_rejected",
        page_path=page_path,
        reverted=reverted,
        frontmatter_cleared=frontmatter_cleared,
    )
    return {
        "page_path": page_path,
        "reverted": reverted,
        "deleted": not reverted,
        "frontmatter_cleared": frontmatter_cleared,
    }


@router.post("/wiki/lint")
async def trigger_lint(
    db: DbSession, settings: AppSettings, project_id: str | None = None
) -> dict[str, object]:
    """FR-43 — manual lint trigger. Serialised with writes (FR-42)."""
    if project_id:
        project = await db.get(Project, project_id)
    else:
        project = (await db.execute(select(Project).limit(1))).scalars().first()
    if project is None:
        raise HTTPException(status_code=404, detail="no project configured")

    queue = get_queue()
    future = queue.submit(f"lint:{project.id}", lambda: run_lint(settings, project))
    result = await future
    return result.to_dict()


@router.post("/wiki/super-summary-proposals/{proposal_id}/approve")
async def approve_super_summary(
    db: DbSession, settings: AppSettings, proposal_id: str
) -> dict[str, object]:
    """FR-41 / AC-8 — regeneration runs only after explicit approval."""
    proposal = await db.get(SuperSummaryProposal, proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail="proposal not found")
    if proposal.status != "pending":
        raise HTTPException(status_code=422, detail=f"proposal is {proposal.status}")

    project = await db.get(Project, proposal.project_id)
    if project is None:  # pragma: no cover
        raise HTTPException(status_code=404, detail="project not found")

    from ..librarian.super_summary import regenerate_super_summary

    queue = get_queue()
    future = queue.submit(
        f"super-summary:{proposal.super_summary_id}",
        lambda: regenerate_super_summary(
            settings, project, proposal.super_summary_id, session_id=proposal.session_id
        ),
    )
    pages = await future

    proposal.status = "approved"
    proposal.resolved_at = utcnow()

    tokens = await log_index_metrics(
        Path(project.wiki_repo_path),
        session_id=proposal.session_id,
        project_id=project.id,
    )
    return {"proposal_id": proposal_id, "pages": pages, "index_token_count": tokens}


@router.post("/wiki/super-summary-proposals/{proposal_id}/reject")
async def reject_super_summary(db: DbSession, proposal_id: str) -> dict[str, object]:
    proposal = await db.get(SuperSummaryProposal, proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail="proposal not found")
    proposal.status = "rejected"
    proposal.resolved_at = utcnow()
    return {"proposal_id": proposal_id, "status": "rejected"}

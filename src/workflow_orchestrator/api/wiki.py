"""Wiki review queue endpoints — SRS FR-40, FR-41, FR-43, §4.3."""

from __future__ import annotations

import difflib
from pathlib import Path

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from ..librarian.layout import layout_for
from ..librarian.lint import run_lint
from ..librarian.queue import get_queue
from ..librarian.writeback import (
    FRONTMATTER,
    has_needs_review,
    log_index_metrics,
)
from ..logging import get_logger
from ..models import Project, SuperSummaryProposal, WikiWrite, utcnow
from .deps import AppSettings, DbSession

log = get_logger(__name__)

router = APIRouter(tags=["wiki"])


def _clear_needs_review(text: str) -> str:
    import re

    match = FRONTMATTER.match(text)
    if not match:
        return text
    block = re.sub(
        r"^needs_review\s*:.*$", "needs_review: false", match.group(1), flags=re.MULTILINE
    )
    return f"---\n{block}\n---\n" + text[match.end() :]


async def _project_for_page(db: DbSession, page_path: str) -> Project | None:
    """Find the project whose wiki repo contains ``page_path``."""
    projects = (await db.execute(select(Project))).scalars().all()
    for project in projects:
        candidate = Path(project.wiki_repo_path) / page_path
        if candidate.exists():
            return project
    return projects[0] if projects else None


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
    project = await _project_for_page(db, page_path)
    if project is None:
        raise HTTPException(status_code=404, detail="no project configured")

    layout = layout_for(project.wiki_repo_path)
    try:
        path = layout.resolve(page_path)
    except ValueError:
        raise HTTPException(status_code=400, detail="path escapes the wiki repo")
    if not path.exists():
        raise HTTPException(status_code=404, detail="page not found")

    current = path.read_text(encoding="utf-8", errors="replace")

    # Compare against the last committed version, which is what "before the
    # automated write" means for a git-backed wiki.
    from ..services.process import run_command

    previous = ""
    result = await run_command(
        ["git", "show", f"HEAD:{page_path}"], cwd=layout.repo_root, timeout=30
    )
    if result.ok:
        previous = result.stdout

    diff = "".join(
        difflib.unified_diff(
            previous.splitlines(keepends=True),
            current.splitlines(keepends=True),
            fromfile=f"a/{page_path}",
            tofile=f"b/{page_path}",
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
    project = await _project_for_page(db, page_path)
    if project is None:
        raise HTTPException(status_code=404, detail="no project configured")

    layout = layout_for(project.wiki_repo_path)
    try:
        path = layout.resolve(page_path)
    except ValueError:
        raise HTTPException(status_code=400, detail="path escapes the wiki repo")
    if not path.exists():
        raise HTTPException(status_code=404, detail="page not found")

    path.write_text(
        _clear_needs_review(path.read_text(encoding="utf-8", errors="replace")),
        encoding="utf-8",
    )

    rows = (
        await db.execute(select(WikiWrite).where(WikiWrite.page_path == page_path))
    ).scalars().all()
    for row in rows:
        row.needs_review = 0

    log.info("wiki.page_approved", page_path=page_path)
    return {"page_path": page_path, "needs_review": False}


@router.post("/wiki/review-queue/{page_path:path}/reject")
async def reject_page(db: DbSession, page_path: str) -> dict[str, object]:
    """Revert an automated write to its committed state."""
    project = await _project_for_page(db, page_path)
    if project is None:
        raise HTTPException(status_code=404, detail="no project configured")

    layout = layout_for(project.wiki_repo_path)
    from ..services.process import run_command

    result = await run_command(
        ["git", "checkout", "--", page_path], cwd=layout.repo_root, timeout=30
    )
    reverted = result.ok
    if not reverted:
        # The page was newly created, so there is nothing to revert to.
        try:
            layout.resolve(page_path).unlink(missing_ok=True)
        except ValueError:
            raise HTTPException(status_code=400, detail="path escapes the wiki repo")

    rows = (
        await db.execute(select(WikiWrite).where(WikiWrite.page_path == page_path))
    ).scalars().all()
    for row in rows:
        row.needs_review = 0

    log.info("wiki.page_rejected", page_path=page_path, reverted=reverted)
    return {"page_path": page_path, "reverted": reverted, "deleted": not reverted}


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

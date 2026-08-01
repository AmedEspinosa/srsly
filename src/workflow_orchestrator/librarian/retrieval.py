"""Librarian retrieval — SRS FR-35, FR-36, FR-37.

At session creation the project's super summary is injected into the session
context. After the first QA prompt, ``wiki-query`` selects relevant pages and
those are loaded too; the set is recorded on ``sessions.wiki_pages_injected``.

Retrieval is best-effort by design: a project whose wiki repo has not been
scaffolded yet must still be able to run a session. Every failure path logs and
returns empty context rather than blocking the QA phase.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..logging import get_logger
from ..models import Project, Session
from ..services.prompts import wiki_context_block
from ..services.workflow import set_wiki_pages
from .layout import layout_for

log = get_logger(__name__)

MAX_PAGE_CHARS = 20_000


def read_super_summary(project: Project) -> str | None:
    """FR-35 — the project's super summary, if the wiki repo has one."""
    layout = layout_for(project.wiki_repo_path)
    try:
        path = layout.resolve(project.wiki_super_summary_path)
    except ValueError:
        log.warning(
            "wiki.super_summary_path_escapes_repo",
            project_id=project.id,
            path=project.wiki_super_summary_path,
        )
        return None

    if not path.exists():
        log.info(
            "wiki.super_summary_missing", project_id=project.id, path=str(path)
        )
        return None
    return path.read_text(encoding="utf-8", errors="replace")[:MAX_PAGE_CHARS]


def read_pages(project: Project, relative_paths: list[str]) -> dict[str, str]:
    layout = layout_for(project.wiki_repo_path)
    pages: dict[str, str] = {}
    for relative in relative_paths:
        try:
            path = layout.resolve(relative)
        except ValueError:
            log.warning("wiki.page_path_escapes_repo", path=relative)
            continue
        if not path.exists():
            log.info("wiki.page_missing", path=str(path))
            continue
        pages[relative] = path.read_text(encoding="utf-8", errors="replace")[:MAX_PAGE_CHARS]
    return pages


async def build_session_context(
    db: AsyncSession, settings: Settings, project: Project, session: Session
) -> str:
    """Assemble the wiki context injected ahead of the first QA question round."""
    layout = layout_for(project.wiki_repo_path)
    if not layout.is_initialized():
        log.info(
            "wiki.not_initialized",
            project_id=project.id,
            wiki_repo_path=project.wiki_repo_path,
            hint="run: workflow-orchestrator wiki init <path>",
        )
        return ""

    super_summary = read_super_summary(project)

    # FR-36 — wiki-query selects the relevant pages for this feature prompt.
    from .query import query_pages

    selected = await query_pages(settings, project, session.feature_prompt)
    pages = read_pages(project, selected)

    # FR-37 — record exactly what was injected.
    set_wiki_pages(session, list(pages.keys()))
    await db.flush()

    log.info(
        "wiki.context_injected",
        session_id=session.id,
        super_summary=bool(super_summary),
        page_count=len(pages),
        pages=list(pages.keys()),
    )
    return wiki_context_block(super_summary, pages)

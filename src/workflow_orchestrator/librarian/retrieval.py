"""Librarian retrieval — SRS FR-35, FR-36, FR-37.

At session creation the project's super summary is injected into the session
context. After the first QA prompt, ``wiki-query`` selects relevant pages and
those are loaded too; the set is recorded on ``sessions.wiki_pages_injected``.

Retrieval is best-effort by design: a project whose wiki repo has not been
scaffolded yet must still be able to run a session. Every failure path logs and
returns empty context rather than blocking the QA phase.

Best-effort is not the same as silent, though, and the two are easy to conflate.
An empty context degrades the requirements engine to generic questions, which
looks like a model problem rather than a configuration one. So the reasons are
captured in a :class:`ContextStatus` that the QA phase reports to the UI, and a
missing super summary logs at warning rather than info.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..logging import get_logger
from ..models import Project, Session
from ..services.prompts import wiki_context_block
from ..services.workflow import set_wiki_pages
from .layout import layout_for

log = get_logger(__name__)

MAX_PAGE_CHARS = 20_000

#: The super summary is the primary context for the QA phase and is expected to
#: be long — the vault this was built against has a 28 KB one. Truncating it to
#: the per-page limit dropped a third of it, so it gets its own, larger cap.
MAX_SUPER_SUMMARY_CHARS = 200_000


@dataclass
class ContextStatus:
    """Why the injected context looks the way it does."""

    wiki_repo_path: str
    resolved_wiki_dir: str
    initialized: bool
    super_summary_path: str
    super_summary_found: bool = False
    super_summary_chars: int = 0
    pages: list[str] = field(default_factory=list)
    hint: str | None = None

    @property
    def has_context(self) -> bool:
        return self.super_summary_found or bool(self.pages)


def _super_summary_path(project: Project) -> Path | None:
    layout = layout_for(project.wiki_repo_path)
    try:
        return layout.resolve(project.wiki_super_summary_path)
    except ValueError:
        log.warning(
            "wiki.super_summary_path_escapes_repo",
            project_id=project.id,
            path=project.wiki_super_summary_path,
        )
        return None


def read_super_summary(project: Project) -> str | None:
    """FR-35 — the project's super summary, if the wiki repo has one."""
    path = _super_summary_path(project)
    if path is None:
        return None

    if not path.exists():
        # Warning, not info: this is the single highest-value piece of context
        # in the QA phase, and its absence is almost always a misconfiguration.
        log.warning(
            "wiki.super_summary_missing", project_id=project.id, path=str(path)
        )
        return None

    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) > MAX_SUPER_SUMMARY_CHARS:
        log.warning(
            "wiki.super_summary_truncated",
            project_id=project.id,
            characters=len(text),
            cap=MAX_SUPER_SUMMARY_CHARS,
        )
    return text[:MAX_SUPER_SUMMARY_CHARS]


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


def inspect_context(project: Project) -> ContextStatus:
    """Report what context *would* be available, without running ``wiki-query``.

    Cheap enough for the UI to call on every page load, so a project pointed at
    the wrong path shows up before a session burns a QA round on it.
    """
    layout = layout_for(project.wiki_repo_path)
    resolved = _super_summary_path(project)
    initialized = layout.is_initialized()

    status = ContextStatus(
        wiki_repo_path=str(project.wiki_repo_path),
        resolved_wiki_dir=str(layout.wiki_dir),
        initialized=initialized,
        super_summary_path=str(resolved) if resolved else project.wiki_super_summary_path,
    )

    if not initialized:
        status.hint = (
            f"no wiki found at {layout.wiki_dir} — point wiki_repo_path at the "
            "directory containing llm-wiki/, or run: "
            "workflow-orchestrator wiki init <path>"
        )
        return status

    if resolved is not None and resolved.exists():
        status.super_summary_found = True
        status.super_summary_chars = min(
            resolved.stat().st_size, MAX_SUPER_SUMMARY_CHARS
        )
    else:
        status.hint = (
            f"super summary not found at {status.super_summary_path} — the QA "
            "phase will ask generic questions without it"
        )
    return status


async def build_session_context(
    db: AsyncSession, settings: Settings, project: Project, session: Session
) -> tuple[str, ContextStatus]:
    """Assemble the wiki context injected ahead of the first QA question round."""
    status = inspect_context(project)
    if not status.initialized:
        log.warning(
            "wiki.not_initialized",
            project_id=project.id,
            wiki_repo_path=project.wiki_repo_path,
            resolved_wiki_dir=status.resolved_wiki_dir,
            hint=status.hint,
        )
        return "", status

    # The super summary is independent of page selection: a wiki with an
    # index.md but no queryable pages still has the most valuable context.
    super_summary = read_super_summary(project)

    # FR-36 — wiki-query selects the relevant pages for this feature prompt.
    from .query import query_pages

    selected = await query_pages(settings, project, session.feature_prompt)
    pages = read_pages(project, selected)

    # FR-37 — record exactly what was injected.
    set_wiki_pages(session, list(pages.keys()))
    await db.flush()

    status.super_summary_found = bool(super_summary)
    status.super_summary_chars = len(super_summary or "")
    status.pages = list(pages.keys())
    if not status.has_context:
        status.hint = (
            "the wiki resolved but contributed nothing — check "
            f"{status.super_summary_path} exists"
        )

    log.info(
        "wiki.context_injected",
        session_id=session.id,
        super_summary=bool(super_summary),
        super_summary_chars=status.super_summary_chars,
        page_count=len(pages),
        pages=status.pages,
    )
    return wiki_context_block(super_summary, pages), status

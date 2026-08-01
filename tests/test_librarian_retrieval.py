"""Librarian retrieval — FR-35, FR-36, FR-37."""

from __future__ import annotations

from pathlib import Path

import pytest

from workflow_orchestrator.config import Settings
from workflow_orchestrator.db import session_scope
from workflow_orchestrator.librarian import query, retrieval
from workflow_orchestrator.librarian.layout import WIKI_ROOT, layout_for
from workflow_orchestrator.librarian.scaffold import init_wiki_repo
from workflow_orchestrator.models import Project, Session

SUMMARY_PATH = f"{WIKI_ROOT}/super-summaries/demo.md"
CONCEPT_PATH = f"{WIKI_ROOT}/concepts/auth.md"


@pytest.fixture
def wiki(wiki_repo: Path) -> Path:
    init_wiki_repo(wiki_repo)
    layout = layout_for(wiki_repo)
    layout.resolve(SUMMARY_PATH).write_text(
        "# Demo super summary\n\nThe service uses Auth0.\n", encoding="utf-8"
    )
    layout.resolve(CONCEPT_PATH).write_text(
        "# Auth\n\nOrg-scoped roles.\n", encoding="utf-8"
    )
    return wiki_repo


def make_project(wiki_root: Path, repo: Path) -> Project:
    return Project(
        id="p1",
        name="demo",
        repo_path=str(repo),
        wiki_repo_path=str(wiki_root),
        wiki_super_summary_path=SUMMARY_PATH,
    )


def test_reads_the_super_summary(wiki: Path, repo: Path) -> None:
    """FR-35 — the project's super summary is available for injection."""
    text = retrieval.read_super_summary(make_project(wiki, repo))
    assert text is not None
    assert "Auth0" in text


def test_missing_super_summary_is_not_fatal(wiki_repo: Path, repo: Path) -> None:
    init_wiki_repo(wiki_repo)
    project = make_project(wiki_repo, repo)
    assert retrieval.read_super_summary(project) is None


def test_super_summary_path_cannot_escape_the_repo(wiki: Path, repo: Path) -> None:
    project = make_project(wiki, repo)
    project.wiki_super_summary_path = "../../../etc/passwd"
    assert retrieval.read_super_summary(project) is None


def test_reads_selected_pages(wiki: Path, repo: Path) -> None:
    pages = retrieval.read_pages(make_project(wiki, repo), [CONCEPT_PATH, "nope.md"])
    assert list(pages) == [CONCEPT_PATH]
    assert "Org-scoped roles" in pages[CONCEPT_PATH]


async def test_context_injection_records_pages_on_the_session(
    db_engine, settings: Settings, wiki: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-36/FR-37 — injected pages are recorded on wiki_pages_injected."""

    async def fake_query(settings_, project_, question: str) -> list[str]:
        assert "Auth0" in question  # the feature prompt reaches wiki-query
        return [CONCEPT_PATH]

    monkeypatch.setattr(retrieval, "query_pages", fake_query, raising=False)
    monkeypatch.setattr(query, "query_pages", fake_query)

    async with session_scope() as db:
        project = make_project(wiki, repo)
        db.add(project)
        session = Session(
            project_id=project.id,
            feature_prompt="Rebuild auth to use Auth0",
            current_phase="qa",
            harness_implement="claude_code",
            harness_review="codex",
        )
        db.add(session)
        await db.flush()

        context = await retrieval.build_session_context(db, settings, project, session)

        # FR-35 — the super summary is in the context.
        assert "Demo super summary" in context
        # FR-36 — so is the selected page.
        assert "Org-scoped roles" in context
        # FR-37 — and the set is recorded on the session.
        from workflow_orchestrator.services.workflow import get_wiki_pages

        assert get_wiki_pages(session) == [CONCEPT_PATH]


async def test_context_is_empty_when_the_wiki_is_not_initialized(
    db_engine, settings: Settings, wiki_repo: Path, repo: Path
) -> None:
    """A session must still be creatable before the wiki repo exists."""
    async with session_scope() as db:
        project = make_project(wiki_repo, repo)  # not scaffolded
        db.add(project)
        session = Session(
            project_id=project.id,
            feature_prompt="anything",
            current_phase="qa",
            harness_implement="claude_code",
            harness_review="codex",
        )
        db.add(session)
        await db.flush()

        assert await retrieval.build_session_context(db, settings, project, session) == ""


def test_context_block_is_marked_as_authoritative_background() -> None:
    from workflow_orchestrator.services.prompts import wiki_context_block

    block = wiki_context_block("summary text", {CONCEPT_PATH: "page text"})
    assert "authoritative background" in block
    assert "summary text" in block
    assert "page text" in block
    assert CONCEPT_PATH in block


def test_empty_context_block_is_empty() -> None:
    from workflow_orchestrator.services.prompts import wiki_context_block

    assert wiki_context_block(None, {}) == ""

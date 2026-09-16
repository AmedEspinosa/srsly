"""Wiki review queue API — FR-40, FR-41, FR-43, AC-8."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from httpx import AsyncClient

from workflow_orchestrator.config import Settings
from workflow_orchestrator.db import session_scope
from workflow_orchestrator.librarian.layout import WIKI_ROOT, layout_for
from workflow_orchestrator.librarian.scaffold import init_wiki_repo
from workflow_orchestrator.models import SuperSummaryProposal, WikiWrite, utcnow

PAGE_PATH = f"{WIKI_ROOT}/concepts/rate-limiting.md"
PAGE_BODY = "---\ntitle: Rate limiting\nneeds_review: true\n---\n\n# Rate limiting\n\nNotes.\n"


def _git_env(root: Path) -> dict[str, str]:
    return {
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
        "HOME": str(root),
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@e",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@e",
    }


def _git_commit(root: Path, message: str) -> None:
    env = _git_env(root)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, env=env, capture_output=True)
    subprocess.run(
        ["git", "commit", "-qm", message], cwd=root, check=True, env=env,
        capture_output=True,
    )


def _git_init(root: Path) -> None:
    subprocess.run(
        ["git", "init", "-q", "-b", "main"], cwd=root, check=True, env=_git_env(root)
    )
    _git_commit(root, "scaffold")


@pytest.fixture
def initialized_wiki(wiki_repo: Path) -> Path:
    """A scaffolded wiki repo under git, with one automated page written."""
    init_wiki_repo(wiki_repo)
    _git_init(wiki_repo)

    page = layout_for(wiki_repo).resolve(PAGE_PATH)
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(PAGE_BODY, encoding="utf-8")
    return wiki_repo


async def add_wiki_write(page_path: str = PAGE_PATH, session_id: str | None = None) -> None:
    async with session_scope() as db:
        db.add(
            WikiWrite(
                session_id=session_id,
                page_path=page_path,
                operation="create",
                needs_review=1,
                index_token_count=1234,
                written_at=utcnow(),
            )
        )


async def test_review_queue_lists_pages_needing_review(
    client: AsyncClient, project: dict, initialized_wiki: Path
) -> None:
    """FR-40 — pages with needs_review: true appear in the queue."""
    await add_wiki_write()
    queue = (await client.get("/wiki/review-queue")).json()
    assert [i["page_path"] for i in queue["items"]] == [PAGE_PATH]
    assert queue["items"][0]["index_token_count"] == 1234


async def test_review_queue_collapses_repeated_writes(
    client: AsyncClient, project: dict, initialized_wiki: Path
) -> None:
    await add_wiki_write()
    await add_wiki_write()
    queue = (await client.get("/wiki/review-queue")).json()
    assert len(queue["items"]) == 1


async def test_diff_shows_the_automated_change(
    client: AsyncClient, project: dict, initialized_wiki: Path
) -> None:
    """FR-40 — inline diff view."""
    await add_wiki_write()
    result = (await client.get(f"/wiki/review-queue/{PAGE_PATH}/diff")).json()
    assert result["needs_review"] is True
    assert "Rate limiting" in result["content"]
    # The page is new, so there is no HEAD version to diff against.
    assert result["is_new"] is True


async def test_approve_clears_the_flag(
    client: AsyncClient, project: dict, initialized_wiki: Path
) -> None:
    """FR-40 — approving clears needs_review in the file and the database."""
    await add_wiki_write()

    response = await client.post(f"/wiki/review-queue/{PAGE_PATH}/approve")
    assert response.status_code == 200

    page = layout_for(initialized_wiki).resolve(PAGE_PATH)
    assert "needs_review: false" in page.read_text()

    queue = (await client.get("/wiki/review-queue")).json()
    assert queue["items"] == []


async def test_reject_removes_a_newly_created_page(
    client: AsyncClient, project: dict, initialized_wiki: Path
) -> None:
    await add_wiki_write()
    result = (await client.post(f"/wiki/review-queue/{PAGE_PATH}/reject")).json()
    assert result["deleted"] is True
    assert not layout_for(initialized_wiki).resolve(PAGE_PATH).exists()

    queue = (await client.get("/wiki/review-queue")).json()
    assert queue["items"] == []


async def test_path_traversal_is_rejected(
    client: AsyncClient, project: dict, initialized_wiki: Path
) -> None:
    """Asserted against the handler, not over HTTP.

    httpx normalises ``..`` out of a URL client-side per RFC 3986, so the
    equivalent request never reached the handler and the 404 came from the
    router — the test passed without exercising any of this code.
    """
    from fastapi import HTTPException

    from workflow_orchestrator.api.wiki import _locate_page

    async with session_scope() as db:
        for escaping in ("../../etc/passwd", f"{WIKI_ROOT}/../../../etc/passwd"):
            with pytest.raises(HTTPException) as caught:
                await _locate_page(db, escaping)
            assert caught.value.status_code == 400


# --- resolving a page to the right wiki repo -----------------------------------


@pytest.fixture
def nested_wiki(wiki_repo: Path) -> Path:
    """A vault that keeps its page directories under ``llm-wiki/wiki/``.

    The shape the real vault uses, and the one the old lookup could not see.
    """
    init_wiki_repo(wiki_repo)
    nested = wiki_repo / WIKI_ROOT / "wiki"
    nested.mkdir(parents=True, exist_ok=True)
    (wiki_repo / WIKI_ROOT / "concepts").rename(nested / "concepts")
    _git_init(wiki_repo)

    page = nested / "concepts" / "rate-limiting.md"
    page.write_text(PAGE_BODY, encoding="utf-8")
    return wiki_repo


async def test_page_is_found_in_a_nested_vault(
    client: AsyncClient, project: dict, nested_wiki: Path
) -> None:
    """A canonical stored path must resolve against a nested vault."""
    await add_wiki_write(PAGE_PATH)
    response = await client.get(f"/wiki/review-queue/{PAGE_PATH}/diff")
    assert response.status_code == 200
    assert "Rate limiting" in response.json()["content"]


async def test_page_is_not_resolved_against_an_unrelated_project(
    client: AsyncClient, project: dict, repo: Path, tmp_path: Path
) -> None:
    """With two distinct vaults, the page must come from the one that has it.

    The old lookup joined the *configured* wiki path with a path stored relative
    to the discovered repo root, so it never matched and always fell back to the
    first project in the table.
    """
    other_root = tmp_path / "other-wiki"
    (other_root / WIKI_ROOT).mkdir(parents=True)
    init_wiki_repo(other_root)
    _git_init(other_root)
    marker = "# Only in the second vault\n"
    page = layout_for(other_root).resolve(PAGE_PATH)
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(f"---\nneeds_review: true\n---\n\n{marker}", encoding="utf-8")

    created = await client.post(
        "/projects",
        json={
            "name": "second",
            "repo_path": str(repo),
            "wiki_repo_path": str(other_root),
            "wiki_super_summary_path": "llm-wiki/super-summaries/second.md",
        },
    )
    assert created.status_code == 201

    await add_wiki_write(PAGE_PATH)
    response = await client.get(f"/wiki/review-queue/{PAGE_PATH}/diff")
    assert response.status_code == 200
    assert marker in response.json()["content"]


async def test_unknown_page_is_404_not_a_guess(
    client: AsyncClient, project: dict, initialized_wiki: Path
) -> None:
    missing = f"{WIKI_ROOT}/concepts/never-written.md"
    await add_wiki_write(missing)
    assert (await client.get(f"/wiki/review-queue/{missing}/diff")).status_code == 404


async def test_reject_clears_the_flag_on_a_committed_page(
    client: AsyncClient, project: dict, initialized_wiki: Path
) -> None:
    """The bug that left 95 pages flagged in the file and reviewed in the queue.

    ``git checkout`` restores the committed version, which carries
    ``needs_review: true`` — so without clearing it afterwards the file and the
    queue disagree forever.
    """
    page = layout_for(initialized_wiki).resolve(PAGE_PATH)
    _git_commit(initialized_wiki, "commit the flagged page")
    assert "needs_review: true" in page.read_text()

    await add_wiki_write()
    result = (await client.post(f"/wiki/review-queue/{PAGE_PATH}/reject")).json()

    assert result["reverted"] is True
    assert result["frontmatter_cleared"] is True
    assert "needs_review: false" in page.read_text()

    queue = (await client.get("/wiki/review-queue")).json()
    assert queue["items"] == []


async def test_approve_adds_the_key_when_frontmatter_lacks_it(
    client: AsyncClient, project: dict, initialized_wiki: Path
) -> None:
    page = layout_for(initialized_wiki).resolve(PAGE_PATH)
    page.write_text("---\ntitle: Rate limiting\n---\n\nBody.\n", encoding="utf-8")
    await add_wiki_write()

    result = (await client.post(f"/wiki/review-queue/{PAGE_PATH}/approve")).json()
    assert result["frontmatter_cleared"] is True
    assert "needs_review: false" in page.read_text()


async def test_approve_leaves_a_page_with_no_frontmatter_alone(
    client: AsyncClient, project: dict, initialized_wiki: Path
) -> None:
    """A page that never had frontmatter was never flagged; do not invent one."""
    page = layout_for(initialized_wiki).resolve(PAGE_PATH)
    page.write_text("# Just a page\n", encoding="utf-8")
    await add_wiki_write()

    result = (await client.post(f"/wiki/review-queue/{PAGE_PATH}/approve")).json()
    assert result["frontmatter_cleared"] is False
    assert page.read_text() == "# Just a page\n"
    # The queue entry is still cleared — the human did review it.
    assert (await client.get("/wiki/review-queue")).json()["items"] == []


# --- FR-41 / AC-8 --------------------------------------------------------------


async def add_proposal(project_id: str) -> str:
    async with session_scope() as db:
        proposal = SuperSummaryProposal(
            project_id=project_id,
            super_summary_id="demo",
            rationale="pages changed",
            status="pending",
            created_at=utcnow(),
        )
        db.add(proposal)
        await db.flush()
        return proposal.id


async def test_ac8_super_summary_is_proposed_not_executed(
    client: AsyncClient, project: dict, initialized_wiki: Path
) -> None:
    """AC-8 — the proposal is surfaced and super-summaries/ is NOT modified."""
    await add_proposal(project["id"])

    queue = (await client.get("/wiki/review-queue")).json()
    assert len(queue["super_summary_proposals"]) == 1
    assert queue["super_summary_proposals"][0]["super_summary_id"] == "demo"

    # Nothing has been written to super-summaries/ without approval.
    summaries = layout_for(initialized_wiki).page_dir("super-summaries")
    assert list(summaries.glob("*.md")) == []


async def test_rejecting_a_proposal_leaves_the_wiki_untouched(
    client: AsyncClient, project: dict, initialized_wiki: Path
) -> None:
    proposal_id = await add_proposal(project["id"])

    result = (
        await client.post(f"/wiki/super-summary-proposals/{proposal_id}/reject")
    ).json()
    assert result["status"] == "rejected"

    summaries = layout_for(initialized_wiki).page_dir("super-summaries")
    assert list(summaries.glob("*.md")) == []

    queue = (await client.get("/wiki/review-queue")).json()
    assert queue["super_summary_proposals"] == []


async def test_approving_a_proposal_twice_is_rejected(
    client: AsyncClient, project: dict, initialized_wiki: Path
) -> None:
    proposal_id = await add_proposal(project["id"])
    await client.post(f"/wiki/super-summary-proposals/{proposal_id}/reject")
    response = await client.post(f"/wiki/super-summary-proposals/{proposal_id}/approve")
    assert response.status_code == 422


async def test_unknown_proposal_404s(client: AsyncClient, project: dict) -> None:
    assert (
        await client.post("/wiki/super-summary-proposals/nope/approve")
    ).status_code == 404


# --- FR-43 ---------------------------------------------------------------------


async def test_lint_reports_when_the_wiki_is_not_initialized(
    client: AsyncClient, project: dict
) -> None:
    """The fixture project's wiki repo has no llm-wiki/ layout."""
    result = (await client.post("/wiki/lint")).json()
    assert result["ok"] is False
    assert "not initialized" in (result["error"] or "")


async def test_wiki_queue_page_renders(client: AsyncClient) -> None:
    page = await client.get("/ui/wiki-queue")
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert "Wiki review queue" in page.text


async def test_api_still_owns_the_review_queue_path(client: AsyncClient) -> None:
    """Regression: the HTML page must not shadow the §4.3 JSON endpoint."""
    response = await client.get("/wiki/review-queue")
    assert response.headers["content-type"].startswith("application/json")

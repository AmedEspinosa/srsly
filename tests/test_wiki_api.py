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


@pytest.fixture
def initialized_wiki(wiki_repo: Path) -> Path:
    """A scaffolded wiki repo under git, with one automated page written."""
    init_wiki_repo(wiki_repo)
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
        "HOME": str(wiki_repo),
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@e",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@e",
    }
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=wiki_repo, check=True, env=env)
    subprocess.run(["git", "add", "-A"], cwd=wiki_repo, check=True, env=env, capture_output=True)
    subprocess.run(
        ["git", "commit", "-qm", "scaffold"], cwd=wiki_repo, check=True, env=env,
        capture_output=True,
    )

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
    response = await client.get("/wiki/review-queue/../../etc/passwd/diff")
    assert response.status_code in (400, 404)


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

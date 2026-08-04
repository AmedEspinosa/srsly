"""Project and session endpoints — FR-1..FR-9."""

from __future__ import annotations

from pathlib import Path

from httpx import AsyncClient
from sqlalchemy import select

from workflow_orchestrator.db import get_sessionmaker
from workflow_orchestrator.models import Session

from .conftest import worktree_for


async def test_create_project_rejects_non_git_path(
    client: AsyncClient, tmp_path: Path, wiki_repo: Path
) -> None:
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    response = await client.post(
        "/projects",
        json={
            "name": "nope",
            "repo_path": str(plain),
            "wiki_repo_path": str(wiki_repo),
            "wiki_super_summary_path": "llm-wiki/super-summaries/x.md",
        },
    )
    assert response.status_code == 400
    assert response.json()["error"] == "not_a_git_repo"


async def test_create_project_and_list(client: AsyncClient, project: dict) -> None:
    assert project["archived_at"] is None
    listed = await client.get("/projects")
    assert [p["id"] for p in listed.json()] == [project["id"]]


async def test_archive_hides_project_from_default_listing(
    client: AsyncClient, project: dict
) -> None:
    await client.post(f"/projects/{project['id']}/archive")
    assert (await client.get("/projects")).json() == []
    archived = (await client.get("/projects?include_archived=true")).json()
    assert archived[0]["archived_at"] is not None


async def test_session_defaults_to_claude_implement_codex_review(
    client: AsyncClient, session: dict
) -> None:
    """FR-8 — the default pairing."""
    assert session["harness_implement"] == "claude_code"
    assert session["harness_review"] == "codex"
    assert session["current_phase"] == "qa"


async def test_session_creates_a_worktree(
    client: AsyncClient, session: dict, repo: Path
) -> None:
    worktree = worktree_for(repo, session["id"])
    assert worktree.is_dir()
    assert (worktree / ".workflow").is_dir()
    # §4.5 — the exclusion goes in .git/info/exclude, which the worktree
    # actually reads, and not in the tracked .gitignore, which it does not.
    exclude = (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert ".workflow/" in exclude
    assert "worktrees/" in exclude
    assert not (repo / ".gitignore").exists(), "the user's repo must not be edited"


async def test_same_harness_claude_code_is_allowed(
    client: AsyncClient, project: dict
) -> None:
    response = await client.post(
        f"/projects/{project['id']}/sessions",
        json={
            "feature_prompt": "anything",
            "harness_implement": "claude_code",
            "harness_review": "claude_code",
        },
    )
    assert response.status_code == 201
    assert response.json()["harness_implement"] == "claude_code"
    assert response.json()["harness_review"] == "claude_code"


async def test_same_harness_codex_is_allowed(
    client: AsyncClient, project: dict
) -> None:
    response = await client.post(
        f"/projects/{project['id']}/sessions",
        json={
            "feature_prompt": "anything",
            "harness_implement": "codex",
            "harness_review": "codex",
        },
    )
    assert response.status_code == 201
    assert response.json()["harness_implement"] == "codex"
    assert response.json()["harness_review"] == "codex"


async def test_same_harness_values_are_persisted(
    client: AsyncClient, project: dict
) -> None:
    response = await client.post(
        f"/projects/{project['id']}/sessions",
        json={
            "feature_prompt": "anything",
            "harness_implement": "claude_code",
            "harness_review": "claude_code",
        },
    )
    assert response.status_code == 201

    async with get_sessionmaker()() as db:
        stored = await db.scalar(select(Session).where(Session.id == response.json()["id"]))

    assert stored is not None
    assert stored.harness_implement == "claude_code"
    assert stored.harness_review == "claude_code"


async def test_reversed_pairing_is_allowed(client: AsyncClient, project: dict) -> None:
    response = await client.post(
        f"/projects/{project['id']}/sessions",
        json={
            "feature_prompt": "anything",
            "harness_implement": "codex",
            "harness_review": "claude_code",
        },
    )
    assert response.status_code == 201
    assert response.json()["harness_implement"] == "codex"


async def test_invalid_implement_harness_returns_422(
    client: AsyncClient, project: dict
) -> None:
    response = await client.post(
        f"/projects/{project['id']}/sessions",
        json={
            "feature_prompt": "anything",
            "harness_implement": "gpt-4o",
            "harness_review": "codex",
        },
    )
    assert response.status_code == 422
    assert any(
        "harness_implement" in str(error["loc"])
        for error in response.json()["detail"]
    )


async def test_invalid_review_harness_returns_422(
    client: AsyncClient, project: dict
) -> None:
    response = await client.post(
        f"/projects/{project['id']}/sessions",
        json={
            "feature_prompt": "anything",
            "harness_implement": "codex",
            "harness_review": "gpt-4o",
        },
    )
    assert response.status_code == 422
    assert any(
        "harness_review" in str(error["loc"])
        for error in response.json()["detail"]
    )


async def test_session_detail_includes_worktree_and_phases(
    client: AsyncClient, session: dict, repo: Path
) -> None:
    detail = (await client.get(f"/sessions/{session['id']}")).json()
    assert detail["worktree_path"] == str(worktree_for(repo, session["id"]))
    assert detail["available_phases"][0] == "qa"
    assert detail["approvals"] == []


async def test_unknown_ids_404(client: AsyncClient) -> None:
    assert (await client.get("/projects/nope")).status_code == 404
    assert (await client.get("/sessions/nope")).status_code == 404

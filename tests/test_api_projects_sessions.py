"""Project and session endpoints — FR-1..FR-9."""

from __future__ import annotations

from pathlib import Path

from httpx import AsyncClient

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
    # §4.5 — .workflow/ must be ignored so agent scratch never reaches a commit.
    gitignore = (repo / ".gitignore").read_text(encoding="utf-8")
    assert ".workflow/" in gitignore
    assert "worktrees/" in gitignore


async def test_same_harness_rejected(client: AsyncClient, project: dict) -> None:
    """AC-3 — 422 {"error": "same_harness_not_allowed"}."""
    response = await client.post(
        f"/projects/{project['id']}/sessions",
        json={
            "feature_prompt": "anything",
            "harness_implement": "claude_code",
            "harness_review": "claude_code",
        },
    )
    assert response.status_code == 422
    assert response.json() == {"error": "same_harness_not_allowed"}


async def test_same_harness_rejected_for_codex_too(
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
    assert response.status_code == 422
    assert response.json() == {"error": "same_harness_not_allowed"}


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

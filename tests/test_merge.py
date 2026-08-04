"""Merge phase — FR-27, FR-28, FR-29, FR-30, AC-10."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from httpx import AsyncClient

from workflow_orchestrator.api.merge import build_pr_body
from workflow_orchestrator.config import Settings
from workflow_orchestrator.models import Harness, Phase
from workflow_orchestrator.services.git import (
    GitService,
    PullRequest,
    branch_name_for,
    pr_title_from_prompt,
)

from .conftest import worktree_for, write_artifact
from .test_api_approvals import advance_to


# --- FR-27: branch naming ------------------------------------------------------


def test_branch_name_matches_the_srs() -> None:
    assert branch_name_for("abc-123") == "workflow/abc-123"


async def test_branch_is_created_at_implement_start_not_at_merge(
    client: AsyncClient, session: dict, repo: Path, settings: Settings
) -> None:
    """FR-27 — "at the start of the implement phase (not at merge)"."""
    write_artifact(repo, session["id"], "srs.md")
    await client.post(f"/sessions/{session['id']}/approve", json={"phase": "srs"})
    write_artifact(repo, session["id"], "plan.md")
    await client.post(f"/sessions/{session['id']}/approve", json={"phase": "plan"})

    worktree = worktree_for(repo, session["id"])
    branches_before = subprocess.run(
        ["git", "branch", "--list", branch_name_for(session["id"])],
        cwd=worktree,
        capture_output=True,
        text=True,
    ).stdout
    assert branches_before.strip() == ""

    # Starting the implement run creates the branch. The run itself needs no
    # runner for this assertion — disable them so the call fails *after* the
    # branch step, which is what we are pinning.
    settings.DOCKER_IMAGE_AGENT = None
    settings.WORKFLOW_ALLOW_HOST_RUNNER = False
    response = await client.post(f"/sessions/{session['id']}/implement/run")
    assert response.status_code == 503  # no runner, but the branch was made

    branches_after = subprocess.run(
        ["git", "branch", "--list", branch_name_for(session["id"])],
        cwd=worktree,
        capture_output=True,
        text=True,
    ).stdout
    assert branch_name_for(session["id"]) in branches_after


# --- FR-28: PR title and body --------------------------------------------------


def test_pr_title_is_truncated_to_72_characters() -> None:
    long_prompt = "Rebuild the authentication subsystem " * 5
    title = pr_title_from_prompt(long_prompt)
    assert len(title) <= 72
    assert title.startswith("Rebuild the authentication subsystem")


def test_pr_title_collapses_whitespace() -> None:
    assert pr_title_from_prompt("  add\n\n  rate   limiting  ") == "add rate limiting"


def test_pr_title_handles_empty_prompt() -> None:
    assert pr_title_from_prompt("   ") == "Workflow session"


class FakeSession:
    id = "sess-1"
    feature_prompt = "Add rate limiting"
    harness_implement = "claude_code"
    harness_review = "codex"


def test_pr_body_does_not_link_uncommitted_artifacts() -> None:
    """Deliberate FR-28 deviation.

    FR-28 says the body links ``srs.md``, ``plan.md`` and ``review.md``. It was
    written assuming those files are in the repository. §4.5 says the opposite —
    session scratch must never reach a commit — and once that was actually
    enforced, the three links pointed at paths that do not exist. PR #49 shipped
    with exactly that table.

    A requirement cannot be met by emitting a link that 404s, so the body names
    where the artifacts really live instead. This is the kind of gap the
    post-merge as-built record exists to write down.
    """
    body = build_pr_body(FakeSession())  # type: ignore[arg-type]

    assert ".workflow/" not in body, "the body must not link uncommitted scratch"
    assert "Add rate limiting" in body
    assert "sess-1" in body
    # The reader still needs to know the artifacts exist and where to find them.
    assert "worktrees/sess-1" in body
    assert "as-built" in body


def test_pr_body_attributes_both_harnesses() -> None:
    """FR-28 — the reviewer must be able to see which backend did what."""
    body = build_pr_body(FakeSession())  # type: ignore[arg-type]
    assert "claude_code" in body
    assert "codex" in body


# --- FR-29/FR-30: merge detection ----------------------------------------------


async def test_merge_completes_the_session(
    client: AsyncClient, session: dict, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-29/FR-30 — a MERGED state completes the session."""
    await advance_to(client, session["id"], repo, Phase.MERGE)
    detail = (await client.get(f"/sessions/{session['id']}")).json()
    assert detail["current_phase"] == "merge"

    async def fake_view(self, worktree, *, ref=None):  # noqa: ANN001
        return PullRequest(number=7, url="https://github.com/o/r/pull/7", state="MERGED")

    monkeypatch.setattr(GitService, "view_pull_request", fake_view)

    result = (await client.get(f"/sessions/{session['id']}/merge/status")).json()
    assert result["merged"] is True

    detail = (await client.get(f"/sessions/{session['id']}")).json()
    assert detail["current_phase"] == "completed"
    assert detail["completed_at"] is not None


async def test_open_pr_state_does_not_complete_the_session(
    client: AsyncClient, session: dict, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await advance_to(client, session["id"], repo, Phase.MERGE)

    async def fake_view(self, worktree, *, ref=None):  # noqa: ANN001
        return PullRequest(number=7, url="https://github.com/o/r/pull/7", state="OPEN")

    monkeypatch.setattr(GitService, "view_pull_request", fake_view)

    result = (await client.get(f"/sessions/{session['id']}/merge/status")).json()
    assert result["merged"] is False
    detail = (await client.get(f"/sessions/{session['id']}")).json()
    assert detail["current_phase"] == "merge"


async def test_merge_is_idempotent(
    client: AsyncClient, session: dict, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repeated polls must not re-complete or re-enqueue write-back."""
    await advance_to(client, session["id"], repo, Phase.MERGE)

    async def fake_view(self, worktree, *, ref=None):  # noqa: ANN001
        return PullRequest(number=7, url="https://github.com/o/r/pull/7", state="MERGED")

    monkeypatch.setattr(GitService, "view_pull_request", fake_view)

    first = (await client.get(f"/sessions/{session['id']}/merge/status")).json()
    second = (await client.get(f"/sessions/{session['id']}/merge/status")).json()
    assert first["merged"] and second["merged"]

    detail = (await client.get(f"/sessions/{session['id']}")).json()
    assert detail["current_phase"] == "completed"


async def test_open_pr_requires_a_remote(
    client: AsyncClient, session: dict, repo: Path
) -> None:
    """The fixture repo has no origin, so this must fail cleanly rather than hang."""
    await advance_to(client, session["id"], repo, Phase.MERGE)
    response = await client.post(f"/sessions/{session['id']}/merge/open-pr")
    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "no_remote"


async def test_merge_blocked_before_review_approval(
    client: AsyncClient, session: dict, repo: Path
) -> None:
    """FR-26 — the merge phase needs a recorded review approval."""
    await advance_to(client, session["id"], repo, Phase.REVIEW)
    response = await client.post(f"/sessions/{session['id']}/merge/open-pr")
    assert response.status_code == 422
    assert response.json() == {"error": "phase_not_ready", "required_approval": "review"}

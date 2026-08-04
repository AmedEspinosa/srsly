"""Regression: ``.workflow/`` scratch reached a pull request — SRS §4.5.

``create_worktree`` appended ``.workflow/`` to the repo's ``.gitignore``, then
immediately ran ``git worktree add --detach <ref>``. A worktree checks out a
*commit*; an uncommitted ``.gitignore`` edit is not in one. So the rule landed
in a file the worktree never read, ``commit_all``'s ``git add -A`` swept the
directory up, and PR #49 on the first real session carried 14 scratch files and
2,226 lines — the SRS, the plan, the review, the run prompts, and a copy of the
PR's own diff.

Those artifacts belong to the wiki, not to the pull request. Two independent
guards enforce that: the exclusion now goes in ``.git/info/exclude`` (which the
worktree does read, and which is never committed), and ``commit_all`` excludes
the directory by pathspec so no ignore-file state can leak it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from httpx import AsyncClient

from workflow_orchestrator.config import Settings
from workflow_orchestrator.services.git import GitService

from .conftest import git, worktree_for


def check_ignored(worktree: Path, relative: str) -> bool:
    result = subprocess.run(
        ["git", "check-ignore", "-q", relative], cwd=worktree, capture_output=True
    )
    return result.returncode == 0


# --- the exclusion lands where the worktree reads it ---------------------------


async def test_workflow_dir_is_ignored_inside_the_worktree(
    client: AsyncClient, session: dict, repo: Path
) -> None:
    """The exact check that failed: run it where the harness actually works."""
    worktree = worktree_for(repo, session["id"])
    (worktree / ".workflow" / "plan.md").write_text("# plan\n", encoding="utf-8")

    assert check_ignored(worktree, ".workflow/plan.md")


async def test_the_users_gitignore_is_left_alone(
    client: AsyncClient, session: dict, repo: Path
) -> None:
    """Creating a session must not dirty a tracked file in someone's repo."""
    assert not (repo / ".gitignore").exists()
    assert subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True
    ).stdout.strip() == ""


async def test_exclude_is_idempotent(repo: Path, settings: Settings) -> None:
    service = GitService(settings)
    await service.ensure_scratch_excluded(repo)
    await service.ensure_scratch_excluded(repo)

    exclude = (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert exclude.count(".workflow/") == 1
    assert exclude.count("worktrees/") == 1


async def test_existing_exclude_content_is_preserved(
    repo: Path, settings: Settings
) -> None:
    exclude = repo / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text("*.secret\n", encoding="utf-8")

    await GitService(settings).ensure_scratch_excluded(repo)

    contents = exclude.read_text(encoding="utf-8")
    assert "*.secret" in contents
    assert ".workflow/" in contents


# --- commit_all excludes it regardless of ignore state -------------------------


@pytest.fixture
def worktree_with_scratch(repo: Path) -> Path:
    """A worktree holding both real work and a full set of session artifacts."""
    worktree = repo / "worktrees" / "sess"
    git("worktree", "add", "--detach", str(worktree), "HEAD", cwd=repo)

    (worktree / "app").mkdir()
    (worktree / "app" / "retry.py").write_text("def retry(): ...\n", encoding="utf-8")

    scratch = worktree / ".workflow"
    scratch.mkdir(exist_ok=True)
    for name in ("srs.md", "plan.md", "review.md", "diff.patch", "run-a.log"):
        (scratch / name).write_text(f"{name} contents\n", encoding="utf-8")
    return worktree


async def test_commit_all_commits_source_but_not_scratch(
    worktree_with_scratch: Path, settings: Settings
) -> None:
    service = GitService(settings)
    assert await service.commit_all(worktree_with_scratch, "feat: retry")

    tracked = subprocess.run(
        ["git", "ls-files"], cwd=worktree_with_scratch, capture_output=True, text=True
    ).stdout.split()
    assert "app/retry.py" in tracked
    assert not [path for path in tracked if path.startswith(".workflow/")]


async def test_scratch_stays_out_even_with_no_ignore_rules_at_all(
    worktree_with_scratch: Path, settings: Settings
) -> None:
    """The pathspec is the guard that does not depend on the target repo.

    We run against repositories we do not control. An ignore file that is
    missing, overridden by a ``!`` rule, or checked out from an older commit
    must not be able to put the SRS back into a pull request.
    """
    exclude = worktree_with_scratch / ".git" / "info" / "exclude"
    if exclude.exists():  # pragma: no cover - only when the fixture pre-wrote it
        exclude.write_text("", encoding="utf-8")
    common = Path(
        subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=worktree_with_scratch,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    common_exclude = common / "info" / "exclude"
    if common_exclude.exists():
        common_exclude.write_text("", encoding="utf-8")
    (worktree_with_scratch / ".gitignore").write_text("!.workflow/\n", encoding="utf-8")

    await GitService(settings).commit_all(worktree_with_scratch, "feat: retry")

    tracked = subprocess.run(
        ["git", "ls-files"], cwd=worktree_with_scratch, capture_output=True, text=True
    ).stdout.split()
    assert not [path for path in tracked if path.startswith(".workflow/")]


async def test_commit_all_reports_nothing_to_do_when_only_scratch_changed(
    worktree_with_scratch: Path, settings: Settings
) -> None:
    """A run that produced only artifacts is not a commit."""
    service = GitService(settings)
    await service.commit_all(worktree_with_scratch, "feat: retry")

    (worktree_with_scratch / ".workflow" / "review.md").write_text(
        "# review round 2\n", encoding="utf-8"
    )

    assert await service.commit_all(worktree_with_scratch, "noop") is False

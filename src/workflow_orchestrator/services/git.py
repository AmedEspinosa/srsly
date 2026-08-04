"""Git and GitHub integration — SRS FR-27, FR-28, FR-29, §4.5.

Worktrees live at ``<repo_root>/worktrees/<session_id>``; each carries a
``.workflow/`` directory holding the session's artifacts. Those artifacts are
kept out of commits two ways — see ``ensure_scratch_excluded`` and
``commit_all`` — because the SRS, plan, review and run logs belong to the wiki,
not to the pull request.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from ..config import Settings
from ..logging import get_logger
from .process import CommandError, run_command

log = get_logger(__name__)

WORKFLOW_DIR = ".workflow"
WORKTREES_DIR = "worktrees"
PR_TITLE_MAX = 72  # FR-28


class GitError(RuntimeError):
    pass


@dataclass(frozen=True)
class PullRequest:
    number: int
    url: str
    state: str


def branch_name_for(session_id: str) -> str:
    """FR-27 — ``workflow/<session_id>``."""
    return f"workflow/{session_id}"


def worktree_path(repo_path: Path | str, session_id: str) -> Path:
    return Path(repo_path) / WORKTREES_DIR / session_id


def workflow_dir(worktree: Path | str) -> Path:
    return Path(worktree) / WORKFLOW_DIR


def pr_title_from_prompt(prompt: str) -> str:
    """Title derived from the feature prompt, truncated to 72 chars (FR-28)."""
    flattened = " ".join(prompt.split())
    if len(flattened) <= PR_TITLE_MAX:
        return flattened or "Workflow session"
    return flattened[: PR_TITLE_MAX - 1].rstrip() + "…"


class GitService:
    def __init__(self, settings: Settings) -> None:
        self._git = settings.WORKFLOW_GIT_BIN
        self._gh = settings.WORKFLOW_GH_BIN

    # --- repository validation ------------------------------------------------

    async def is_git_repo(self, repo_path: Path | str) -> bool:
        path = Path(repo_path)
        if not path.is_dir():
            return False
        result = await run_command(
            [self._git, "rev-parse", "--is-inside-work-tree"], cwd=path
        )
        return result.ok and result.stdout.strip() == "true"

    async def current_branch(self, repo_path: Path | str) -> str:
        result = await run_command(
            [self._git, "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_path
        )
        return result.check().stdout.strip()

    async def has_commits(self, repo_path: Path | str) -> bool:
        result = await run_command([self._git, "rev-parse", "HEAD"], cwd=repo_path)
        return result.ok

    # --- worktrees ------------------------------------------------------------

    async def ensure_scratch_excluded(self, repo_path: Path | str) -> None:
        """Ignore ``.workflow/`` and ``worktrees/`` via ``.git/info/exclude`` (§4.5).

        Not ``.gitignore``. That file is tracked, so writing to it (a) modifies
        the user's repository as a side effect of creating a session and (b) has
        no effect where it matters: ``git worktree add --detach <ref>`` checks
        out a commit, and an uncommitted ``.gitignore`` edit is not in it. The
        rule was written to a file the worktree would never read, which is how
        14 scratch files reached a pull request.

        ``info/exclude`` resolves to the *common* git dir, so one write covers
        every worktree of the repo, and it is never committed.
        """
        repo = Path(repo_path)
        result = await run_command(
            [self._git, "rev-parse", "--git-path", "info/exclude"], cwd=repo
        )
        if not result.ok:
            log.warning("exclude.unresolved", repo_path=str(repo))
            return
        # Relative when run from the repo root, absolute inside a worktree.
        exclude = Path(result.stdout.strip())
        if not exclude.is_absolute():
            exclude = repo / exclude

        existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        lines = {line.strip() for line in existing.splitlines()}
        additions = [
            entry
            for entry in (f"{WORKFLOW_DIR}/", f"{WORKTREES_DIR}/")
            if entry not in lines
        ]
        if not additions:
            return
        exclude.parent.mkdir(parents=True, exist_ok=True)
        prefix = "" if existing.endswith("\n") or not existing else "\n"
        exclude.write_text(
            existing + prefix + "\n".join(additions) + "\n", encoding="utf-8"
        )
        log.info("exclude.updated", path=str(exclude), added=additions)

    async def create_worktree(
        self, repo_path: Path | str, session_id: str, *, base: str | None = None
    ) -> Path:
        """Create ``worktrees/<session_id>`` and seed its ``.workflow/`` directory.

        The worktree is created detached at ``base`` (default: current HEAD).
        The session branch is created later, at implement-phase start (FR-27).
        """
        repo = Path(repo_path)
        target = worktree_path(repo, session_id)
        if target.exists():
            return target

        await self.ensure_scratch_excluded(repo)
        target.parent.mkdir(parents=True, exist_ok=True)

        ref = base or "HEAD"
        result = await run_command(
            [self._git, "worktree", "add", "--detach", str(target), ref], cwd=repo
        )
        if not result.ok:
            raise GitError(
                f"could not create worktree for session {session_id}: "
                f"{(result.stderr or result.stdout).strip()}"
            )

        workflow_dir(target).mkdir(parents=True, exist_ok=True)
        log.info("worktree.created", session_id=session_id, path=str(target))
        return target

    async def remove_worktree(self, repo_path: Path | str, session_id: str) -> None:
        repo = Path(repo_path)
        target = worktree_path(repo, session_id)
        if not target.exists():
            return
        result = await run_command(
            [self._git, "worktree", "remove", "--force", str(target)], cwd=repo
        )
        if not result.ok:
            shutil.rmtree(target, ignore_errors=True)
            await run_command([self._git, "worktree", "prune"], cwd=repo)
        log.info("worktree.removed", session_id=session_id)

    # --- branch / commit / push ----------------------------------------------

    async def create_branch(self, worktree: Path | str, branch: str) -> str:
        """FR-27 — create the session branch at the start of the implement phase."""
        existing = await run_command(
            [self._git, "rev-parse", "--verify", branch], cwd=worktree
        )
        if existing.ok:
            checkout = await run_command([self._git, "checkout", branch], cwd=worktree)
            checkout.check()
        else:
            created = await run_command([self._git, "checkout", "-b", branch], cwd=worktree)
            created.check()
        log.info("branch.created", branch=branch, worktree=str(worktree))
        return branch

    async def status_porcelain(self, worktree: Path | str) -> list[str]:
        result = await run_command([self._git, "status", "--porcelain"], cwd=worktree)
        result.check()
        return [line for line in result.stdout.splitlines() if line.strip()]

    async def changed_paths(self, worktree: Path | str) -> list[str]:
        """Paths reported by ``git status --porcelain``, sans status prefix."""
        paths: list[str] = []
        for line in await self.status_porcelain(worktree):
            entry = line[3:] if len(line) > 3 else line
            # Renames render as "old -> new"; the destination is what matters.
            if " -> " in entry:
                entry = entry.split(" -> ", 1)[1]
            paths.append(entry.strip().strip('"'))
        return paths

    async def diff(self, worktree: Path | str, *, base: str | None = None) -> str:
        argv = [self._git, "diff"]
        if base:
            argv.append(base)
        result = await run_command(argv, cwd=worktree, timeout=180.0)
        result.check()
        untracked = await run_command(
            [self._git, "ls-files", "--others", "--exclude-standard"], cwd=worktree
        )
        extra = ""
        if untracked.ok and untracked.stdout.strip():
            names = [n for n in untracked.stdout.splitlines() if n.strip()]
            extra = "\n".join(f"?? {name}" for name in names)
        return result.stdout + ("\n" + extra if extra else "")

    async def commit_all(self, worktree: Path | str, message: str) -> bool:
        """Stage and commit the work, never the scratch.

        The ``.workflow/`` exclusion is a pathspec rather than a reliance on
        ignore rules: this runs against repositories we do not control, and an
        ignore file that is missing, overridden, or checked out from an older
        commit must not be able to leak the SRS, plan, review and run logs into
        a pull request. Returns False when there was nothing to commit.
        """
        await run_command(
            [self._git, "add", "-A", "--", ".", f":(exclude){WORKFLOW_DIR}"],
            cwd=worktree,
        )
        staged = await run_command([self._git, "diff", "--cached", "--quiet"], cwd=worktree)
        if staged.returncode == 0:
            return False
        result = await run_command([self._git, "commit", "-m", message], cwd=worktree)
        result.check()
        return True

    async def push_branch(self, worktree: Path | str, branch: str) -> None:
        result = await run_command(
            [self._git, "push", "--set-upstream", "origin", branch], cwd=worktree, timeout=300.0
        )
        if not result.ok:
            raise GitError(
                f"could not push branch {branch}: {(result.stderr or result.stdout).strip()}"
            )
        log.info("branch.pushed", branch=branch)

    async def has_remote(self, worktree: Path | str) -> bool:
        result = await run_command([self._git, "remote"], cwd=worktree)
        return result.ok and bool(result.stdout.strip())

    # --- GitHub ---------------------------------------------------------------

    async def create_pull_request(
        self, worktree: Path | str, *, title: str, body: str, base: str | None = None
    ) -> PullRequest:
        """FR-28 — ``gh pr create``."""
        argv = [self._gh, "pr", "create", "--title", title, "--body", body]
        if base:
            argv += ["--base", base]
        result = await run_command(argv, cwd=worktree, timeout=180.0)
        if not result.ok:
            raise GitError(
                f"gh pr create failed: {(result.stderr or result.stdout).strip()}"
            )
        url = result.stdout.strip().splitlines()[-1].strip()
        view = await self.view_pull_request(worktree)
        if view is not None:
            return view
        return PullRequest(number=0, url=url, state="OPEN")

    async def view_pull_request(
        self, worktree: Path | str, *, ref: str | None = None
    ) -> PullRequest | None:
        """FR-29 — ``gh pr view --json state``."""
        argv = [self._gh, "pr", "view"]
        if ref:
            argv.append(ref)
        argv += ["--json", "number,url,state"]
        result = await run_command(argv, cwd=worktree, timeout=60.0)
        if not result.ok:
            return None
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            return None
        return PullRequest(
            number=int(payload.get("number") or 0),
            url=str(payload.get("url") or ""),
            state=str(payload.get("state") or "UNKNOWN"),
        )

    async def pull_request_diff(
        self, worktree: Path | str, number: int
    ) -> str | None:
        """``gh pr diff`` — what the pull request actually landed.

        Authoritative in a way the local branch is not: it survives squash
        merges, maintainer edits and commits pushed to the PR after we last
        looked. Returns None rather than raising — an as-built record derived
        from the code alone is still worth more than no record.
        """
        result = await run_command(
            [self._gh, "pr", "diff", str(number)], cwd=worktree, timeout=180.0
        )
        if not result.ok:
            log.warning(
                "gh.pr_diff_failed",
                number=number,
                error=(result.stderr or result.stdout).strip()[-300:],
            )
            return None
        return result.stdout

    async def comment_on_pull_request(
        self, worktree: Path | str, number: int, body: str
    ) -> bool:
        """``gh pr comment``. Returns False on failure rather than raising."""
        result = await run_command(
            [self._gh, "pr", "comment", str(number), "--body", body],
            cwd=worktree,
            timeout=120.0,
        )
        if not result.ok:
            log.warning(
                "gh.pr_comment_failed",
                number=number,
                error=(result.stderr or result.stdout).strip()[-300:],
            )
            return False
        log.info("gh.pr_commented", number=number)
        return True

    async def gh_available(self) -> bool:
        try:
            result = await run_command([self._gh, "--version"], timeout=15.0)
        except (FileNotFoundError, CommandError, TimeoutError):
            return False
        return result.ok

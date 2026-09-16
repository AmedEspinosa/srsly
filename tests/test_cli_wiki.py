"""``wiki reconcile`` — repairing needs_review labels that drifted from the queue.

The drift is real and was measured: rejecting a page runs ``git checkout``,
which restores a version committed *with* ``needs_review: true`` while the
database row is zeroed. The file then says "unreviewed" and the queue says
"handled", and nothing reconciles them.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from workflow_orchestrator.cli import app
from workflow_orchestrator.librarian.layout import WIKI_ROOT, layout_for
from workflow_orchestrator.librarian.scaffold import init_wiki_repo

FLAGGED = "---\ntitle: {title}\nneeds_review: true\n---\n\nBody.\n"
REVIEWED = "---\ntitle: {title}\nneeds_review: false\n---\n\nBody.\n"


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """A wiki holding one page of each bucket, plus a decoy outside it."""
    root = tmp_path / "notes"
    (root / WIKI_ROOT).mkdir(parents=True)
    init_wiki_repo(root)
    layout = layout_for(root)

    for name, body in (
        ("stale", FLAGGED),  # bucket A: flagged, but the queue says reviewed
        ("unknown", FLAGGED),  # bucket B: flagged, no queue row at all
        ("clean", REVIEWED),  # agrees with the queue
    ):
        page = layout.page_dir("concepts") / f"{name}.md"
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(body.format(title=name), encoding="utf-8")

    # A personal note that happens to use the same frontmatter key. It lives
    # outside llm-wiki/ and must never be touched or even reported.
    decoy = root / "personal" / "diary.md"
    decoy.parent.mkdir(parents=True, exist_ok=True)
    decoy.write_text(FLAGGED.format(title="diary"), encoding="utf-8")
    return root


@pytest.fixture
def seeded_db(tmp_path: Path, vault: Path) -> Path:
    """A migrated database with one project and one dispositioned write."""
    import subprocess
    import sys

    db_path = tmp_path / "db.sqlite3"
    result = subprocess.run(
        [sys.executable, "-m", "workflow_orchestrator.cli", "db", "migrate"],
        capture_output=True,
        text=True,
        env=_env(db_path),
    )
    assert result.returncode == 0, result.stderr

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO projects (id, name, repo_path, wiki_repo_path,"
            " wiki_super_summary_path, created_at)"
            " VALUES ('p1', 'p', ?, ?, 'llm-wiki/super-summaries/p.md',"
            " '2026-01-01T00:00:00Z')",
            (str(tmp_path / "repo"), str(vault)),
        )
        conn.execute(
            "INSERT INTO wiki_writes (id, session_id, page_path, operation,"
            " needs_review, written_at)"
            " VALUES ('w1', NULL, ?, 'create', 0, '2026-01-02T00:00:00Z')",
            (f"{WIKI_ROOT}/concepts/stale.md",),
        )
    return db_path


def _env(db_path: Path) -> dict[str, str]:
    return {
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
        "HOME": str(db_path.parent),
        "WORKFLOW_DB_PATH": str(db_path),
        "WORKFLOW_LOG_DIR": str(db_path.parent / "logs"),
        "AWS_REGION": "us-east-1",
        "AWS_BEARER_TOKEN_BEDROCK": "test-token",
    }


def _run(db_path: Path, *args: str, monkeypatch: pytest.MonkeyPatch):
    for key, value in _env(db_path).items():
        monkeypatch.setenv(key, value)
    from workflow_orchestrator.config import reset_settings_cache

    reset_settings_cache()
    return CliRunner().invoke(app, ["wiki", "reconcile", *args])


def _page(vault: Path, name: str) -> Path:
    return layout_for(vault).page_dir("concepts") / f"{name}.md"


def test_reconcile_is_dry_run_by_default(
    vault: Path, seeded_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This writes into a personal notes repo; the default must never be write."""
    result = _run(seeded_db, monkeypatch=monkeypatch)

    assert result.exit_code == 0, result.output
    assert "stale label" in result.output
    assert "concepts/stale.md" in result.output
    assert "--apply" in result.output
    assert "needs_review: true" in _page(vault, "stale").read_text()


def test_reconcile_apply_clears_stale_labels(
    vault: Path, seeded_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _run(seeded_db, "--apply", "--allow-dirty", monkeypatch=monkeypatch)

    assert result.exit_code == 0, result.output
    assert "needs_review: false" in _page(vault, "stale").read_text()


def test_reconcile_never_touches_pages_with_no_queue_row(
    vault: Path, seeded_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The database has no opinion on these, so neither does the tool.

    Clearing them would record a review that never happened.
    """
    result = _run(seeded_db, "--apply", "--allow-dirty", monkeypatch=monkeypatch)

    assert result.exit_code == 0, result.output
    assert "unknown provenance" in result.output
    assert "needs_review: true" in _page(vault, "unknown").read_text()


def test_reconcile_ignores_files_outside_the_wiki_directory(
    vault: Path, seeded_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A vault usually sits inside a larger notes repo full of unrelated markdown."""
    result = _run(seeded_db, "--apply", "--allow-dirty", monkeypatch=monkeypatch)

    assert result.exit_code == 0, result.output
    assert "diary" not in result.output
    assert "needs_review: true" in (vault / "personal" / "diary.md").read_text()


def test_adopt_enqueues_pages_of_unknown_provenance(
    vault: Path, seeded_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The honest outcome: do not claim they were reviewed, put them in the queue."""
    result = _run(seeded_db, "--apply", "--adopt", "--allow-dirty", monkeypatch=monkeypatch)
    assert result.exit_code == 0, result.output

    with sqlite3.connect(seeded_db) as conn:
        queued = {
            row[0]
            for row in conn.execute(
                "SELECT page_path FROM wiki_writes WHERE needs_review = 1"
            )
        }
    assert f"{WIKI_ROOT}/concepts/unknown.md" in queued


def test_reconcile_refuses_to_apply_on_a_dirty_repo(
    vault: Path, seeded_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clean tree is what makes ``git checkout .`` a complete undo."""
    import subprocess

    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
        "HOME": str(vault),
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@e",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@e",
    }
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=vault, check=True, env=env)
    subprocess.run(["git", "add", "-A"], cwd=vault, check=True, env=env, capture_output=True)
    subprocess.run(
        ["git", "commit", "-qm", "seed"], cwd=vault, check=True, env=env, capture_output=True
    )
    _page(vault, "clean").write_text("dirty\n", encoding="utf-8")

    result = _run(seeded_db, "--apply", monkeypatch=monkeypatch)
    assert result.exit_code == 2
    assert "uncommitted changes" in result.output
    assert "needs_review: true" in _page(vault, "stale").read_text()

"""Regression: ``db migrate`` must be idempotent.

``migrations/env.py`` ran ``PRAGMA journal_mode=WAL`` on the connection before
configuring Alembic. Under SQLAlchemy 2.0 that opens an implicit transaction,
so Alembic's own transaction nested inside it and the ``alembic_version`` write
rolled back when the connection closed. SQLite commits DDL eagerly, so the
tables appeared and the first run looked successful — but the version table
stayed empty, and the second run re-ran ``0001`` and died on CREATE TABLE.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest


def _migrate(db_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "workflow_orchestrator.cli", "db", "migrate"],
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(db_path.parent),
            "WORKFLOW_DB_PATH": str(db_path),
            "AWS_REGION": "us-east-1",
            "AWS_BEARER_TOKEN_BEDROCK": "test-token",
        },
    )


def _alembic(db_path: Path, action: str, revision: str) -> None:
    from alembic import command
    from alembic.config import Config

    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option(
        "script_location", str(Path(__file__).resolve().parents[1] / "migrations")
    )
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    getattr(command, action)(config, revision)


def _insert_project_and_session(
    conn: sqlite3.Connection,
    session_id: str,
    harness_implement: str,
    harness_review: str,
) -> None:
    conn.execute(
        "INSERT INTO projects "
        "(id, name, repo_path, wiki_repo_path, wiki_super_summary_path, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("project", "project", "/repo", "/wiki", "summary.md", "2026-08-04T00:00:00Z"),
    )
    conn.execute(
        "INSERT INTO sessions "
        "(id, project_id, feature_prompt, current_phase, harness_implement, "
        "harness_review, wiki_pages_injected, created_at, wiki_context_injected) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            session_id,
            "project",
            "feature",
            "qa",
            harness_implement,
            harness_review,
            "[]",
            "2026-08-04T00:00:00Z",
            0,
        ),
    )


def test_migrate_is_idempotent_and_records_its_version(tmp_path: Path) -> None:
    db_path = tmp_path / "db.sqlite3"

    first = _migrate(db_path)
    assert first.returncode == 0, first.stderr

    with sqlite3.connect(db_path) as conn:
        versions = conn.execute("SELECT version_num FROM alembic_version").fetchall()
    assert len(versions) == 1, "alembic_version must be committed, not rolled back"
    assert versions[0][0] == "0004"

    second = _migrate(db_path)
    assert second.returncode == 0, second.stderr
    assert "CREATE TABLE" not in second.stderr


def test_head_schema_has_every_mapped_session_column(tmp_path: Path) -> None:
    """The migrations and the ORM must not drift — a missing column only shows
    up at runtime as a 500 on session create."""
    from workflow_orchestrator.models import Session

    db_path = tmp_path / "db.sqlite3"
    assert _migrate(db_path).returncode == 0

    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
        table_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'sessions'"
        ).fetchone()[0]
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(sessions)")}

    mapped = {c.name for c in Session.__table__.columns}
    assert mapped <= columns, f"missing from migrations: {sorted(mapped - columns)}"
    assert "ck_sessions_cross_harness" not in table_sql
    assert "ck_sessions_phase" in table_sql
    assert "ck_sessions_harness_implement" in table_sql
    assert "ck_sessions_harness_review" in table_sql
    assert "ix_sessions_project_id" in indexes


def test_upgrade_preserves_existing_cross_harness_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "db.sqlite3"
    _alembic(db_path, "upgrade", "0002")
    with sqlite3.connect(db_path) as conn:
        _insert_project_and_session(conn, "session", "claude_code", "codex")
        before = conn.execute(
            "SELECT id, project_id, feature_prompt, current_phase, harness_implement, "
            "harness_review, wiki_pages_injected, created_at, completed_at, branch_name, "
            "pr_url, pr_number, pr_state, wiki_context_injected FROM sessions"
        ).fetchall()

    _alembic(db_path, "upgrade", "head")
    with sqlite3.connect(db_path) as conn:
        after = conn.execute(
            "SELECT id, project_id, feature_prompt, current_phase, harness_implement, "
            "harness_review, wiki_pages_injected, created_at, completed_at, branch_name, "
            "pr_url, pr_number, pr_state, wiki_context_injected FROM sessions"
        ).fetchall()
        conn.execute(
            "INSERT INTO sessions "
            "(id, project_id, feature_prompt, current_phase, harness_implement, "
            "harness_review, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("same", "project", "feature", "qa", "codex", "codex", "now"),
        )

    assert after == before


def test_downgrade_fails_safely_for_same_harness_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "db.sqlite3"
    assert _migrate(db_path).returncode == 0
    with sqlite3.connect(db_path) as conn:
        _insert_project_and_session(conn, "session", "codex", "codex")

    with pytest.raises(Exception, match="ck_sessions_cross_harness"):
        _alembic(db_path, "downgrade", "0002")

    # Alembic applies each step in order: 0004 -> 0003 succeeds and is stamped,
    # then 0003 -> 0002 raises. The database is left at the last step that
    # completed, with the session row intact.
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0003"
        assert conn.execute("SELECT id FROM sessions").fetchall() == [("session",)]
        table_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'sessions'"
        ).fetchone()[0]
    assert "ck_sessions_cross_harness" not in table_sql


def test_downgrade_restores_cross_harness_check_when_safe(tmp_path: Path) -> None:
    db_path = tmp_path / "db.sqlite3"
    assert _migrate(db_path).returncode == 0
    with sqlite3.connect(db_path) as conn:
        _insert_project_and_session(conn, "session", "claude_code", "codex")

    _alembic(db_path, "downgrade", "0002")
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0002"
        table_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'sessions'"
        ).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO sessions "
                "(id, project_id, feature_prompt, current_phase, harness_implement, "
                "harness_review, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("same", "project", "feature", "qa", "codex", "codex", "now"),
            )
    assert "ck_sessions_cross_harness" in table_sql


def test_table_copy_migration_handles_10000_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "db.sqlite3"
    _alembic(db_path, "upgrade", "0002")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO projects "
            "(id, name, repo_path, wiki_repo_path, wiki_super_summary_path, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("project", "project", "/repo", "/wiki", "summary.md", "now"),
        )
        conn.executemany(
            "INSERT INTO sessions "
            "(id, project_id, feature_prompt, current_phase, harness_implement, "
            "harness_review, created_at, wiki_context_injected) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                (f"session-{n}", "project", "feature", "qa", "claude_code", "codex", "now", 0)
                for n in range(10_000)
            ),
        )

    started = time.perf_counter()
    _alembic(db_path, "upgrade", "head")
    elapsed = time.perf_counter() - started

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 10_000
    assert elapsed < 5.0

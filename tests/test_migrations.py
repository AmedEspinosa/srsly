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
from pathlib import Path


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


def test_migrate_is_idempotent_and_records_its_version(tmp_path: Path) -> None:
    db_path = tmp_path / "db.sqlite3"

    first = _migrate(db_path)
    assert first.returncode == 0, first.stderr

    with sqlite3.connect(db_path) as conn:
        versions = conn.execute("SELECT version_num FROM alembic_version").fetchall()
    assert len(versions) == 1, "alembic_version must be committed, not rolled back"

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

    mapped = {c.name for c in Session.__table__.columns}
    assert mapped <= columns, f"missing from migrations: {sorted(mapped - columns)}"


# --- 0003: the write-back claim and its backfills -------------------------------


def _alembic_config(db_path: Path):
    """Alembic driven in-process — the CLI exposes no downgrade command."""
    from alembic.config import Config

    ini = Path(__file__).resolve().parents[1] / "alembic.ini"
    config = Config(str(ini))
    config.set_main_option("script_location", str(ini.parent / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return config


def _upgrade(db_path: Path, revision: str = "head") -> None:
    from alembic import command

    command.upgrade(_alembic_config(db_path), revision)


def _downgrade(db_path: Path, revision: str) -> None:
    from alembic import command

    command.downgrade(_alembic_config(db_path), revision)


def _seed_sessions(db_path: Path) -> None:
    """Three sessions mirroring the states 0003 has to tell apart."""
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO projects (id, name, repo_path, wiki_repo_path,"
            " wiki_super_summary_path, created_at)"
            " VALUES ('p1', 'p', '/tmp/r', '/tmp/w', 'llm-wiki/x.md', '2026-01-01T00:00:00Z')"
        )
        rows = [
            # already ingested: has wiki_writes rows
            ("ingested", "completed", "2026-02-01T00:00:00Z", "MERGED"),
            # completed against a terminal PR, but wrote nothing
            ("terminal", "completed", "2026-02-02T00:00:00Z", "MERGED"),
            # completed by hand while the PR is still open — the recovery case
            ("open-pr", "completed", "2026-02-03T00:00:00Z", "OPEN"),
        ]
        for sid, phase, completed, pr_state in rows:
            conn.execute(
                "INSERT INTO sessions (id, project_id, feature_prompt, current_phase,"
                " harness_implement, harness_review, wiki_pages_injected, created_at,"
                " completed_at, pr_url, pr_number, pr_state)"
                " VALUES (?, 'p1', 'f', ?, 'claude_code', 'codex', '[]',"
                " '2026-01-01T00:00:00Z', ?, 'https://x/1', 1, ?)",
                (sid, phase, completed, pr_state),
            )
        for written_at in ("2026-03-01T00:00:00Z", "2026-03-05T00:00:00Z"):
            conn.execute(
                "INSERT INTO wiki_writes (id, session_id, page_path, operation,"
                " needs_review, written_at)"
                " VALUES (?, 'ingested', ?, 'create', 0, ?)",
                (f"w-{written_at}", f"llm-wiki/{written_at}.md", written_at),
            )


def _writeback_stamps(db_path: Path) -> dict[str, str | None]:
    with sqlite3.connect(db_path) as conn:
        return {
            row[0]: row[1]
            for row in conn.execute("SELECT id, wiki_writeback_at FROM sessions")
        }


def test_0003_backfills_only_the_sessions_that_are_really_done(tmp_path: Path) -> None:
    """The backfill is what stops the widened poller re-ingesting old sessions.

    Without it, every previously merged session is selected on the first tick
    after upgrade and re-runs the as-built harness — a paid call — plus a full
    re-ingest.
    """
    db_path = tmp_path / "db.sqlite3"
    _upgrade(db_path)
    # Reset to 0002 so the column is added over seeded data, as it will be live.
    _downgrade(db_path, "0002")
    _seed_sessions(db_path)
    _upgrade(db_path)

    stamps = _writeback_stamps(db_path)
    # 1 — stamped from the newest write, not from completed_at.
    assert stamps["ingested"] == "2026-03-05T00:00:00Z"
    # 2 — completed against a terminal PR having written nothing.
    assert stamps["terminal"] == "2026-02-02T00:00:00Z"
    # Deliberately left NULL: this is the session the fix exists to recover.
    assert stamps["open-pr"] is None


def test_0003_downgrade_preserves_the_session_check_constraints(tmp_path: Path) -> None:
    """Pins the decision not to use batch mode.

    ``op.batch_alter_table`` rebuilds the table from SQLite's reflected schema,
    and Alembic's reflection of SQLite CHECK constraints is lossy — the four
    named §5.1 constraints would vanish silently.
    """
    db_path = tmp_path / "db.sqlite3"
    _upgrade(db_path)

    def checks() -> int:
        with sqlite3.connect(db_path) as conn:
            ddl = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='sessions'"
            ).fetchone()[0]
        return ddl.count("ck_sessions")

    assert checks() == 4
    _downgrade(db_path, "0002")

    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
    assert "wiki_writeback_at" not in columns
    assert checks() == 4

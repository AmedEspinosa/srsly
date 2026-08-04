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

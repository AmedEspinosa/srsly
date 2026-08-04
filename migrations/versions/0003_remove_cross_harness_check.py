"""Remove the cross-harness CHECK constraint from ``sessions``.

Revision ID: 0003
Revises: 0002

WARNING (downgrade): ``downgrade()`` raises ``sqlite3.IntegrityError`` if any
session has the same implementation and review harness at that time. The
operation is transactional, so no data is lost on failure; same-harness rows
must be removed or updated before the downgrade can succeed.
"""

from __future__ import annotations

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def _create_sessions_table(*, include_cross_harness_check: bool) -> None:
    cross_harness_check = (
        "CONSTRAINT ck_sessions_cross_harness "
        "CHECK (harness_implement != harness_review),"
    )
    if not include_cross_harness_check:
        cross_harness_check = ""

    op.execute(
        f"""
        CREATE TABLE sessions_new (
            id VARCHAR NOT NULL,
            project_id VARCHAR NOT NULL,
            feature_prompt TEXT NOT NULL,
            current_phase VARCHAR NOT NULL,
            harness_implement VARCHAR NOT NULL,
            harness_review VARCHAR NOT NULL,
            wiki_pages_injected TEXT DEFAULT '[]' NOT NULL,
            created_at VARCHAR NOT NULL,
            completed_at VARCHAR,
            branch_name VARCHAR,
            pr_url VARCHAR,
            pr_number INTEGER,
            pr_state VARCHAR,
            wiki_context_injected BOOLEAN DEFAULT 0 NOT NULL,
            PRIMARY KEY (id),
            CONSTRAINT ck_sessions_phase CHECK (
                current_phase IN ('qa','srs','plan','implement','review','merge','completed')
            ),
            CONSTRAINT ck_sessions_harness_implement CHECK (
                harness_implement IN ('claude_code','codex')
            ),
            CONSTRAINT ck_sessions_harness_review CHECK (
                harness_review IN ('claude_code','codex')
            ),
            {cross_harness_check}
            FOREIGN KEY(project_id) REFERENCES projects (id)
        )
        """
    )


_SESSION_COLUMNS = (
    "id, project_id, feature_prompt, current_phase, harness_implement, "
    "harness_review, wiki_pages_injected, created_at, completed_at, branch_name, "
    "pr_url, pr_number, pr_state, wiki_context_injected"
)


def _replace_sessions_table(*, include_cross_harness_check: bool) -> None:
    op.execute("DROP TABLE IF EXISTS sessions_new")
    _create_sessions_table(include_cross_harness_check=include_cross_harness_check)
    op.execute(
        f"INSERT INTO sessions_new ({_SESSION_COLUMNS}) "
        f"SELECT {_SESSION_COLUMNS} FROM sessions"
    )
    op.execute("DROP TABLE sessions")
    op.execute("ALTER TABLE sessions_new RENAME TO sessions")
    op.create_index("ix_sessions_project_id", "sessions", ["project_id"])


def upgrade() -> None:
    _replace_sessions_table(include_cross_harness_check=False)


def downgrade() -> None:
    _replace_sessions_table(include_cross_harness_check=True)

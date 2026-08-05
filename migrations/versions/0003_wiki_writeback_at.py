"""Decouple the wiki write-back from session completion.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-05

``completed_at`` was doing two jobs: recording that the session was finished,
and gating the post-merge wiki write-back. A session completed by hand — which
the UI offers in every phase, including ``merge`` — therefore locked itself out
of ingestion permanently, because both guards read "completed" as "already
handled". This column records the second fact on its own.

Two backfills, and the second is the one that matters:

  1. Sessions that already have ``wiki_writes`` rows have run. Stamped from the
     newest ``written_at`` — the moment the write-back actually happened, which
     is more truthful than ``completed_at``.

  2. Sessions closed out against a terminal pull request (MERGED/CLOSED) that
     wrote nothing. Without this, the widened poller predicate re-selects every
     previously merged session on the first tick after upgrade and re-runs
     ``generate_as_built`` — a paid harness invocation — plus a full re-ingest.
     Their write-back either ran and produced nothing, or is no longer
     reconstructible; either way re-running it is not a repair.

What is deliberately left NULL: sessions whose pull request is still open.
Those are exactly the ones this change exists to recover.

Not folded in: ``wiki_writes.project_id``. It was considered and does not fix
the review-queue lookup it looks like it would — two projects can share one
wiki repo, so a project id does not identify a vault, and the lookup resolves
to a ``WikiLayout`` instead. It would fix a different bug (page paths colliding
across genuinely distinct vaults in ``approve_page``/``reject_page``), which
deserves its own change and its own tests.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sessions",
        sa.Column("wiki_writeback_at", sa.String(), nullable=True),
    )

    # 1 — sessions the Librarian has already ingested.
    op.execute(
        sa.text(
            """
            UPDATE sessions
               SET wiki_writeback_at = (
                     SELECT MAX(w.written_at)
                       FROM wiki_writes w
                      WHERE w.session_id = sessions.id
                   )
             WHERE EXISTS (
                     SELECT 1 FROM wiki_writes w WHERE w.session_id = sessions.id
                   )
            """
        )
    )

    # 2 — sessions closed out against a terminal pull request that wrote nothing.
    op.execute(
        sa.text(
            """
            UPDATE sessions
               SET wiki_writeback_at = completed_at
             WHERE wiki_writeback_at IS NULL
               AND completed_at IS NOT NULL
               AND UPPER(COALESCE(pr_state, '')) IN ('MERGED', 'CLOSED')
            """
        )
    )


def downgrade() -> None:
    # Plain drop_column, matching 0002. ``render_as_batch`` in migrations/env.py
    # is an autogenerate *rendering* option — it does not wrap hand-written ops —
    # and wrapping this in ``op.batch_alter_table`` would be actively harmful:
    # batch mode recreates the table from SQLite's reflected schema, and
    # Alembic's reflection of SQLite CHECK constraints is lossy, so the four
    # named ``ck_sessions_*`` constraints would be silently dropped.
    op.drop_column("sessions", "wiki_writeback_at")

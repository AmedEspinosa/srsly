"""Record whether a session's QA loop opened with wiki context.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-01

Sessions created before this column ran without it, so the default of ``0`` is
also the truthful backfill — there is no history to reconstruct.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sessions",
        sa.Column(
            "wiki_context_injected",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )


def downgrade() -> None:
    op.drop_column("sessions", "wiki_context_injected")

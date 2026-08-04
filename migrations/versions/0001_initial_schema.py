"""Initial schema — SRS §5.1

Revision ID: 0001
Revises:
Create Date: 2026-07-31
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

PHASES = "('qa','srs','plan','implement','review','merge','completed')"
HARNESSES = "('claude_code','codex')"
RUN_STATUSES = "('pending','running','completed','failed','timed_out','cost_exceeded')"
WIKI_OPS = "('create','update')"


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("repo_path", sa.String(), nullable=False),
        sa.Column("wiki_repo_path", sa.String(), nullable=False),
        sa.Column("wiki_super_summary_path", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("archived_at", sa.String(), nullable=True),
    )

    op.create_table(
        "sessions",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("feature_prompt", sa.Text(), nullable=False),
        sa.Column("current_phase", sa.String(), nullable=False),
        sa.Column("harness_implement", sa.String(), nullable=False),
        sa.Column("harness_review", sa.String(), nullable=False),
        sa.Column("wiki_pages_injected", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("completed_at", sa.String(), nullable=True),
        sa.Column("branch_name", sa.String(), nullable=True),
        sa.Column("pr_url", sa.String(), nullable=True),
        sa.Column("pr_number", sa.Integer(), nullable=True),
        sa.Column("pr_state", sa.String(), nullable=True),
        sa.CheckConstraint(f"current_phase IN {PHASES}", name="ck_sessions_phase"),
        sa.CheckConstraint(
            f"harness_implement IN {HARNESSES}", name="ck_sessions_harness_implement"
        ),
        sa.CheckConstraint(f"harness_review IN {HARNESSES}", name="ck_sessions_harness_review"),
        # FR-9 / AC-3
        sa.CheckConstraint("harness_implement != harness_review", name="ck_sessions_cross_harness"),
    )
    op.create_index("ix_sessions_project_id", "sessions", ["project_id"])

    op.create_table(
        "artifacts",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("session_id", sa.String(), sa.ForeignKey("sessions.id"), nullable=False),
        sa.Column("phase", sa.String(), nullable=False),
        sa.Column("file_path", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
    )
    op.create_index("ix_artifacts_session_id", "artifacts", ["session_id"])

    op.create_table(
        "approvals",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("session_id", sa.String(), sa.ForeignKey("sessions.id"), nullable=False),
        sa.Column("phase", sa.String(), nullable=False),
        sa.Column("artifact_id", sa.String(), sa.ForeignKey("artifacts.id"), nullable=False),
        sa.Column("approved_by", sa.String(), nullable=False, server_default="user"),
        sa.Column("approved_at", sa.String(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
    )
    op.create_index("ix_approvals_session_id", "approvals", ["session_id"])

    op.create_table(
        "runs",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("session_id", sa.String(), sa.ForeignKey("sessions.id"), nullable=False),
        sa.Column("phase", sa.String(), nullable=False),
        sa.Column("harness", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("container_id", sa.String(), nullable=True),
        sa.Column("log_path", sa.String(), nullable=True),
        sa.Column("started_at", sa.String(), nullable=True),
        sa.Column("ended_at", sa.String(), nullable=True),
        sa.Column("tokens_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0"),
        sa.CheckConstraint(f"status IN {RUN_STATUSES}", name="ck_runs_status"),
        sa.CheckConstraint(f"harness IN {HARNESSES}", name="ck_runs_harness"),
    )
    op.create_index("ix_runs_session_id", "runs", ["session_id"])
    op.create_index("ix_runs_status", "runs", ["status"])

    op.create_table(
        "wiki_writes",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("session_id", sa.String(), sa.ForeignKey("sessions.id"), nullable=True),
        sa.Column("page_path", sa.String(), nullable=False),
        sa.Column("operation", sa.String(), nullable=False),
        sa.Column("needs_review", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("index_token_count", sa.Integer(), nullable=True),
        sa.Column("written_at", sa.String(), nullable=False),
        sa.CheckConstraint(f"operation IN {WIKI_OPS}", name="ck_wiki_writes_operation"),
    )
    op.create_index("ix_wiki_writes_needs_review", "wiki_writes", ["needs_review"])

    op.create_table(
        "super_summary_proposals",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("session_id", sa.String(), sa.ForeignKey("sessions.id"), nullable=True),
        sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("super_summary_id", sa.String(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("resolved_at", sa.String(), nullable=True),
    )

    op.create_table(
        "qa_turns",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("session_id", sa.String(), sa.ForeignKey("sessions.id"), nullable=False),
        sa.Column("round_number", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
    )
    op.create_index("ix_qa_turns_session_id", "qa_turns", ["session_id"])


def downgrade() -> None:
    op.drop_table("qa_turns")
    op.drop_table("super_summary_proposals")
    op.drop_table("wiki_writes")
    op.drop_table("runs")
    op.drop_table("approvals")
    op.drop_table("artifacts")
    op.drop_table("sessions")
    op.drop_table("projects")

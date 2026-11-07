"""Create durable proposal-call reservations.

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"


def upgrade() -> None:
    op.create_table(
        "proposal_budget",
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("runs.id"), primary_key=True),
        sa.Column("limit_usd", sa.Numeric(24, 12), nullable=False),
        sa.CheckConstraint("limit_usd > 0", name="ck_proposal_budget_positive"),
    )
    op.create_table(
        "proposal_calls",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("proposal_budget.run_id"), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("reserved_usd", sa.Numeric(24, 12), nullable=False),
        sa.Column("actual_usd", sa.Numeric(24, 12)),
        sa.Column("input_rate", sa.Numeric(24, 12), nullable=False),
        sa.Column("output_rate", sa.Numeric(24, 12), nullable=False),
        sa.Column("input_limit", sa.BigInteger(), nullable=False),
        sa.Column("output_limit", sa.BigInteger(), nullable=False),
        sa.Column("input_tokens", sa.BigInteger()),
        sa.Column("output_tokens", sa.BigInteger()),
        sa.Column("rate_provenance", sa.Text(), nullable=False),
        sa.Column("failure_reason", sa.Text()),
        sa.CheckConstraint(
            "status IN ('reserved', 'settled', 'unknown')", name="ck_proposal_call_status"
        ),
        sa.CheckConstraint("reserved_usd >= 0", name="ck_proposal_call_reserved"),
        sa.CheckConstraint("actual_usd IS NULL OR actual_usd >= 0", name="ck_proposal_call_actual"),
    )
    op.create_index("ix_proposal_calls_run_id", "proposal_calls", ["run_id"])

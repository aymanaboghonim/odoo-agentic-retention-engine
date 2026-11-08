"""Persist worker claims and external-effect attempts.

Revision ID: 0007
Revises: 0006
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"


def upgrade() -> None:
    op.create_table(
        "execution_claims",
        sa.Column(
            "run_id", sa.Uuid(), sa.ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("claim_id", sa.Uuid(), nullable=False, unique=True),
        sa.Column("binding_hash", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "execution_effects",
        sa.Column(
            "run_id",
            sa.Uuid(),
            sa.ForeignKey("execution_claims.run_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("effect", sa.Text(), primary_key=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("reference", sa.Text()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("effect IN ('coupon', 'message')"),
        sa.CheckConstraint("status IN ('started', 'confirmed', 'unknown', 'not_sent')"),
        sa.CheckConstraint("attempts > 0 AND attempts <= max_attempts"),
    )

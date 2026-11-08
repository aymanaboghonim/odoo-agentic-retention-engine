"""Persist an aggregate budget shared by proposal runs.

Revision ID: 0008
Revises: 0007
"""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"


def upgrade() -> None:
    op.create_table(
        "proposal_review_budgets",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("limit_usd", sa.Numeric(24, 12), nullable=False),
        sa.CheckConstraint("limit_usd > 0"),
    )
    op.add_column(
        "proposal_calls",
        sa.Column("review_id", sa.Text(), sa.ForeignKey("proposal_review_budgets.id")),
    )
    op.create_index("ix_proposal_calls_review_id", "proposal_calls", ["review_id"])

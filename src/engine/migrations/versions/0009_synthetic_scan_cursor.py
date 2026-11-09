"""Persist the synthetic review scan cursor.

Revision ID: 0009
Revises: 0008
"""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"


def upgrade() -> None:
    op.create_table(
        "synthetic_scan_cursors",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("after_id", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("after_id >= 0"),
    )

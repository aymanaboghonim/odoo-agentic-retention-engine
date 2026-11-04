"""Create the safety-gate budget and coupon ledger.

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"


def upgrade() -> None:
    op.create_table(
        "gate_budget_days",
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("reserved", sa.Numeric(14, 2), nullable=False),
        sa.CheckConstraint("reserved >= 0", name="ck_gate_budget_reserved_non_negative"),
    )
    op.create_table(
        "gate_coupon_reservations",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("customer_id", sa.Text(), nullable=False),
        sa.Column("cart_id", sa.Text(), nullable=False),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("day", "customer_id", "cart_id", name="uq_gate_coupon_cart_day"),
    )

"""Add a mandatory expiry to approval requests (#86, spec AA-5).

Revision ID: 0012
Revises: 0011
"""

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"


def upgrade() -> None:
    op.add_column(
        "approval_requests", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True)
    )
    # Existing approvals predate the TTL rule: treat them as already expired (fail closed).
    op.execute("UPDATE approval_requests SET expires_at = created_at WHERE expires_at IS NULL")
    op.alter_column("approval_requests", "expires_at", nullable=False)
    op.create_index("ix_approval_requests_expires_at", "approval_requests", ["expires_at"])

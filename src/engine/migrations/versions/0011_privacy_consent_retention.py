"""Add Q12 consent, erasure tombstones, and run retention columns.

Revision ID: 0011
Revises: 0010
"""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"


def upgrade() -> None:
    op.add_column("runs", sa.Column("customer_id", sa.Text(), nullable=True))
    op.add_column(
        "runs", sa.Column("retention_expires_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index("ix_runs_customer_id", "runs", ["customer_id"])
    op.create_index("ix_runs_retention_expires_at", "runs", ["retention_expires_at"])
    op.create_table(
        "customer_consents",
        sa.Column("customer_id", sa.Text(), primary_key=True),
        sa.Column("channel", sa.Text(), primary_key=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("channel IN ('email', 'telegram')", name="ck_consent_channel"),
        sa.CheckConstraint("status IN ('opted_in', 'opted_out')", name="ck_consent_status"),
    )
    op.create_table(
        "erasure_requests",
        sa.Column("subject_hash", sa.Text(), primary_key=True),
        sa.Column("erased_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("runs_deleted", sa.Integer(), nullable=False),
        sa.Column("reconsented_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("length(subject_hash) = 64", name="ck_erasure_subject_hash"),
        sa.CheckConstraint("runs_deleted >= 0", name="ck_erasure_runs_deleted"),
    )

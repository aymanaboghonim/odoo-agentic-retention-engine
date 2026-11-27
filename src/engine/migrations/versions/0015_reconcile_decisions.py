"""Reconciliation evidence, admin decisions, and orphan-coupon deactivation (#132, ADR-0030).

Revision ID: 0015
Revises: 0014
"""

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"


def upgrade() -> None:
    # The worker stage stores the latest evidence for the held-runs API (REC-11, #131 note).
    op.add_column("execution_effects", sa.Column("evidence", sa.Text(), nullable=True))
    op.add_column(
        "execution_effects", sa.Column("evidence_at", sa.DateTime(timezone=True), nullable=True)
    )
    # REC-10: set once the worker has deactivated a proven orphan coupon.
    op.add_column(
        "execution_effects",
        sa.Column("coupon_deactivated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_effect_evidence",
        "execution_effects",
        "evidence IS NULL OR evidence IN ('present', 'absent', 'unknown', 'attestation_required')",
    )
    # REC-7: the API records an admin decision; the worker applies it (ADR-0012).
    op.create_table(
        "reconcile_decisions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "run_id", sa.Uuid(), sa.ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("effect", sa.Text(), nullable=False),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("basis", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.CheckConstraint("effect IN ('coupon', 'message')", name="ck_decision_effect"),
        sa.CheckConstraint("decision IN ('confirmed', 'not_sent')", name="ck_decision_value"),
        sa.CheckConstraint("basis IN ('evidence', 'attestation')", name="ck_decision_basis"),
        sa.CheckConstraint(
            "(closed_at IS NULL AND outcome IS NULL) OR (closed_at IS NOT NULL AND outcome IN "
            "('applied', 'rejected_mismatch', 'not_held'))",
            name="ck_decision_outcome",
        ),
    )
    # One open decision per held effect (REC-8).
    op.create_index(
        "ux_reconcile_decisions_open",
        "reconcile_decisions",
        ["run_id", "effect"],
        unique=True,
        postgresql_where=sa.text("closed_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ux_reconcile_decisions_open", table_name="reconcile_decisions")
    op.drop_table("reconcile_decisions")
    op.drop_constraint("ck_effect_evidence", "execution_effects", type_="check")
    for column in ("coupon_deactivated_at", "evidence_at", "evidence"):
        op.drop_column("execution_effects", column)

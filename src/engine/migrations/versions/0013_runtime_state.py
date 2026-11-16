"""Add the durable engine runtime state and its audit trail (#87, spec KS-1).

Revision ID: 0013
Revises: 0012
"""

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"


def upgrade() -> None:
    op.create_table(
        "engine_runtime_state",
        sa.Column("id", sa.SmallInteger(), primary_key=True),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("changed_by", sa.Text(), nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_runtime_state_singleton"),
        sa.CheckConstraint(
            "state IN ('active', 'fallback_fixed_rule', 'halted')", name="ck_runtime_state_value"
        ),
        sa.CheckConstraint("version >= 1", name="ck_runtime_state_version"),
    )
    op.execute(
        "INSERT INTO engine_runtime_state (id, state, reason, changed_by, changed_at, version) "
        "VALUES (1, 'active', 'initial state', 'migration', now(), 1)"
    )
    op.create_table(
        "engine_runtime_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("from_state", sa.Text(), nullable=False),
        sa.Column("to_state", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
    )
    # The circuit breaker reads run_events inside a time window.
    op.create_index("ix_run_events_at", "run_events", ["at"])

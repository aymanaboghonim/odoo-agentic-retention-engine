"""Store the deterministic email Message-ID on the message effect (#131, REC-2, ADR-0030).

Revision ID: 0014
Revises: 0013
"""

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"


def upgrade() -> None:
    # Set when the attempt starts, before the send; NULL for Telegram and for rows before #131.
    op.add_column("execution_effects", sa.Column("message_id", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("execution_effects", "message_id")

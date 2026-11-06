"""Create the derived policy retrieval index.

Revision ID: 0003
Revises: 0002
"""

from alembic import op

revision = "0003"
down_revision = "0002"


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute(
        """
        CREATE TABLE policy_passages (
            source_id text NOT NULL,
            language text NOT NULL CHECK (language IN ('en', 'ar')),
            version text NOT NULL,
            content text NOT NULL,
            content_hash text NOT NULL,
            model_id text NOT NULL,
            embedding vector(1024) NOT NULL,
            search_vector tsvector NOT NULL,
            PRIMARY KEY (source_id, language)
        )
        """
    )
    op.execute("CREATE INDEX policy_passages_search ON policy_passages USING gin (search_vector)")

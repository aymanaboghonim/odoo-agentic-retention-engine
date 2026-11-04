from __future__ import annotations

from alembic import context
from sqlalchemy import Connection

connection: Connection = context.config.attributes["connection"]
context.configure(connection=connection, target_metadata=None)
with context.begin_transaction():
    context.run_migrations()

"""Runs the data store tree on the connection it's given (`aios.datastores.provision`), which is
already on the store's database as its owner role."""

from sqlalchemy import text

from aios.datastores.migrations import context_options
from aios.datastores.models import SCHEMA
from alembic import context

connection = context.config.attributes["connection"]
if connection is None:
    raise RuntimeError("The data store tree runs only on a store's connection (see provision.py)")

# The version table lives in `aios`, so the schema has to exist before Alembic looks for it.
connection.execute(text(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}"))
connection.commit()

context.configure(connection=connection, **context_options())
with context.begin_transaction():
    context.run_migrations()

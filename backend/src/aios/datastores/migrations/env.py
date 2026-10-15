"""Runs the data store tree on the connection it's given (`aios.datastores.provision`), which is
already on the store's database as its owner role."""

from sqlalchemy import text

from alembic import context

SCHEMA = "aios"


def include_name(name, type_, parent_names) -> bool:
    # The owner's tables (`data`) are changed only by approved schema change requests.
    return not (type_ == "schema" and name == "data")


connection = context.config.attributes["connection"]
if connection is None:
    raise RuntimeError("The data store tree runs only on a store's connection (see provision.py)")

# The version table lives in `aios`, so the schema has to exist before Alembic looks for it.
connection.execute(text(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}"))
connection.commit()

context.configure(
    connection=connection,
    target_metadata=None,
    version_table_schema=SCHEMA,
    include_schemas=True,
    include_name=include_name,
)
with context.begin_transaction():
    context.run_migrations()

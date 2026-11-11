from logging.config import fileConfig

from sqlalchemy import create_engine, pool

from aios.config import get_settings
from aios.models import Base
from alembic import context

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# The app's tables, in the `public` schema. LangGraph's checkpointer keeps its own tables in the
# `langgraph` schema and migrates them itself (`aios.checkpoints.setup`); autogenerate only
# compares the default schema, so it doesn't see them. The app's role has `langgraph` on its
# search path (0057), and the default schema's tables are those visible on it, so migrations run
# with `public` alone (the platform database is connected directly, never through the pooler).
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=get_settings().database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(get_settings().database_url, poolclass=pool.NullPool)
    with connectable.connect() as connection:
        connection.exec_driver_sql('SET search_path = "$user", public')
        connection.commit()
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

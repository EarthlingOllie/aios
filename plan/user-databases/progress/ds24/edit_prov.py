p = "tests/test_provisioning.py"
s = open(p).read()
s += '''

def test_the_store_tree_makes_the_tables_the_store_models_define(db, new_user):
    """Like `alembic check` for the platform: a provisioned store's tables are what
    `aios.datastores.models` says, so the models and the tree can't drift apart."""
    store_id = data_store_for(new_user().id)
    provision.provision(store_id)
    names = provision.StoreNames.of(store_row(db, store_id))
    url = make_url(get_settings().data_store_provisioner_url).set(database=names.dbname)
    engine = create_engine(url, poolclass=pool.NullPool)
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql(f'SET ROLE "{names.owner}"')
            context = MigrationContext.configure(conn, opts=migrations.context_options())
            assert compare_metadata(context, StoreBase.metadata) == []
            tables = conn.scalars(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'aios'")
            ).all()
            assert {"documents", "chunks"} <= set(tables)
    finally:
        engine.dispose()
'''


def rep(a, b):
    global s
    assert a in s, a
    s = s.replace(a, b)


rep("from sqlalchemy import select, text, update\n", "from alembic.autogenerate import compare_metadata\nfrom alembic.migration import MigrationContext\nfrom sqlalchemy import create_engine, pool, select, text, update\n")
rep("from aios.datastores.migrations import head_revision\n", "from aios.datastores.migrations import head_revision\nfrom aios.datastores.models import StoreBase\n")
open(p, "w").write(s)

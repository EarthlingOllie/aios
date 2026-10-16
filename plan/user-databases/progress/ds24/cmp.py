from sqlalchemy import create_engine, pool
from sqlalchemy.engine import make_url

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from aios.config import get_settings
from aios.datastores import data_store_for, migrations, provision
from aios.datastores.models import StoreBase
from aios.db import SessionLocal
from aios.models import DataStore, User

with SessionLocal() as db:
    u = User(github_id=-777, login="x")
    db.add(u)
    db.commit()
    sid = data_store_for(u.id)
    provision.provision(sid)
    names = provision.StoreNames.of(db.get(DataStore, sid))
    try:
        url = make_url(get_settings().data_store_provisioner_url).set(database=names.dbname)
        e = create_engine(url, poolclass=pool.NullPool, connect_args={"options": "-c search_path=extensions"})
        with e.connect() as c:
            c.exec_driver_sql(f'SET ROLE "{names.owner}"')
            from sqlalchemy import inspect
            i = inspect(c)
            print(i.default_schema_name, i.get_schema_names(), i.get_table_names(schema="aios"))
            ctx = MigrationContext.configure(c, opts=migrations.context_options())
            for d in compare_metadata(ctx, StoreBase.metadata):
                print(repr(d)[:300])
        e.dispose()
    finally:
        db.delete(u)
        db.commit()
        provision.drop(names)

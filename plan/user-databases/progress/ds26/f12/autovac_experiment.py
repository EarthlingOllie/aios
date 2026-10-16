"""Does an autovacuum worker in a store's database make provision.drop fail?

Provisions a store, makes a big table in it (as the superuser) that autovacuum will work on
slowly, waits for an autovacuum worker to be in the database, then calls provision.drop.
"""

import os
import time

import psycopg
from sqlalchemy import delete

from aios.config import libpq_conninfo
from aios.datastores import data_store_for, provision
from aios.db import SessionLocal
from aios.models import DataStore, User

admin_url = os.environ["DATABASE_ADMIN_URL"]

with SessionLocal() as db:
    u = User(github_id=-777777, login="autovac-experiment")
    db.add(u)
    db.commit()
    store_id = data_store_for(u.id)
    user_id = u.id
provision.provision(store_id)
with SessionLocal() as db:
    names = provision.StoreNames.of(db.get(DataStore, store_id))

try:
    with psycopg.connect(libpq_conninfo(admin_url, database=names.dbname), autocommit=True) as c:
        c.execute(
            "CREATE TABLE aios.big (i int) WITH (autovacuum_vacuum_cost_delay = 100,"
            " autovacuum_vacuum_cost_limit = 1, autovacuum_analyze_threshold = 1)"
        )
        c.execute("INSERT INTO aios.big SELECT generate_series(1, 200000)")
        c.execute("DELETE FROM aios.big WHERE i % 2 = 0")
    with psycopg.connect(libpq_conninfo(admin_url), autocommit=True) as watch:
        deadline = time.monotonic() + 60
        while True:
            rows = watch.execute(
                "SELECT pid, backend_type, usesysid, usename, query FROM pg_stat_activity"
                " WHERE datname = %s",
                (names.dbname,),
            ).fetchall()
            if any(r[1] == "autovacuum worker" for r in rows):
                break
            assert time.monotonic() < deadline, "no autovacuum worker came"
            time.sleep(0.1)
        print("in the store's database:", rows)
        t = time.monotonic()
        try:
            provision.drop(names)
            print(f"drop succeeded in {time.monotonic() - t:.2f}s")
        except Exception as exc:  # noqa: BLE001
            print(f"drop failed after {time.monotonic() - t:.2f}s: {type(exc).__name__}: {exc}")
finally:
    provision.drop(names) if False else None
    with psycopg.connect(libpq_conninfo(admin_url), autocommit=True) as c:
        c.execute(f'DROP DATABASE IF EXISTS "{names.dbname}" WITH (FORCE)')
        for r in (names.rw, names.ro, names.owner):
            c.execute(f'DROP ROLE IF EXISTS "{r}"')
    with SessionLocal() as db:
        db.execute(delete(User).where(User.id == user_id))
        db.commit()

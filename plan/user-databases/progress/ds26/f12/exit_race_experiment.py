"""Does a login-role session that the client just closed (its backend still exiting) make
`DROP DATABASE ... WITH (FORCE)` fail for the provisioner? Run with autovacuum off.

Per try: provision is too slow, so one store is reused: connect as ro, run a query, close,
then immediately try the FORCE drop of a *copy*... simpler: we only test the permission check,
so try `DROP DATABASE <store> WITH (FORCE)` inside a transaction-less probe isn't possible
without dropping. Instead each try recreates the database from the template as the owner.
"""

import os
import sys

import psycopg
from psycopg import errors, sql
from sqlalchemy import delete

from aios.config import get_settings, libpq_conninfo
from aios.datastores import credentials, data_store_for, provision
from aios.db import SessionLocal
from aios.models import DataStore, User

admin_url = os.environ["DATABASE_ADMIN_URL"]
prov_url = get_settings().data_store_provisioner_url
tries = int(sys.argv[1])

with SessionLocal() as db:
    u = User(github_id=-777778, login="race-experiment")
    db.add(u)
    db.commit()
    store_id = data_store_for(u.id)
    user_id = u.id
provision.provision(store_id)
with SessionLocal() as db:
    store = db.get(DataStore, store_id)
    names = provision.StoreNames.of(store)
    ro_password = credentials.decrypt(store.ro_password)

ro_kw = dict(dbname=names.dbname, user=names.ro, password=ro_password)
failed = 0
try:
    with psycopg.connect(libpq_conninfo(prov_url), autocommit=True) as prov:
        for i in range(tries):
            if i:
                prov.execute(
                    sql.SQL("CREATE DATABASE {} OWNER {} TEMPLATE {}").format(
                        sql.Identifier(names.dbname),
                        sql.Identifier(names.owner),
                        sql.Identifier(get_settings().data_store_template),
                    )
                )
                prov.execute(
                    sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                        sql.Identifier(names.dbname), sql.Identifier(names.ro)
                    )
                )
            with psycopg.connect(libpq_conninfo(prov_url), autocommit=True, **ro_kw) as ro:
                ro.execute("SELECT 1")
            try:
                prov.execute(
                    sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(names.dbname))
                )
            except errors.InsufficientPrivilege:
                failed += 1
                with psycopg.connect(libpq_conninfo(admin_url), autocommit=True) as a:
                    a.execute(
                        sql.SQL("DROP DATABASE {} WITH (FORCE)").format(
                            sql.Identifier(names.dbname)
                        )
                    )
    print(f"{failed} of {tries} FORCE drops right after closing a ro session were refused")
finally:
    with psycopg.connect(libpq_conninfo(admin_url), autocommit=True) as c:
        c.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(names.dbname)))
        for r in (names.rw, names.ro, names.owner):
            c.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(r)))
    with SessionLocal() as db:
        db.execute(delete(User).where(User.id == user_id))
        db.commit()

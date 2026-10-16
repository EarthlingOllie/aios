"""provision.drop right after closing a ro session (backend possibly still exiting), N times."""

import sys

import psycopg
from sqlalchemy import delete

from aios.config import get_settings, libpq_conninfo
from aios.datastores import credentials, data_store_for, provision
from aios.db import SessionLocal
from aios.models import DataStore, User

prov_url = get_settings().data_store_provisioner_url
failed = 0
n = int(sys.argv[1])
for i in range(n):
    with SessionLocal() as db:
        u = User(github_id=-880000 - i, login=f"loop-{i}")
        db.add(u)
        db.commit()
        store_id = data_store_for(u.id)
        user_id = u.id
    provision.provision(store_id)
    with SessionLocal() as db:
        store = db.get(DataStore, store_id)
        names = provision.StoreNames.of(store)
        pw = credentials.decrypt(store.ro_password)
    with psycopg.connect(
        libpq_conninfo(prov_url), dbname=names.dbname, user=names.ro, password=pw, autocommit=True
    ) as ro:
        ro.execute("SELECT 1")
    try:
        provision.drop(names)
    except Exception as exc:  # noqa: BLE001
        failed += 1
        print(type(exc).__name__, exc)
        provision.drop(names)
    with SessionLocal() as db:
        db.execute(delete(User).where(User.id == user_id))
        db.commit()
print(f"{failed} of {n} drops right after closing a ro session failed")

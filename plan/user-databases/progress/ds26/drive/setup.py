"""Stand in for an approved schema change: make `data.fish` in the check user's store, as its
owner role. Also provision another user's store with `data.secrets`, and print its database
name, for the isolation check.
"""

import psycopg
from sqlalchemy import select

from aios.config import get_settings, libpq_conninfo
from aios.datastores import data_store_for, provision
from aios.db import SessionLocal
from aios.models import DataStore, User


def as_owner(store: DataStore) -> psycopg.Connection:
    url = get_settings().data_store_provisioner_url
    conn = psycopg.connect(libpq_conninfo(url, database=store.dbname), autocommit=True)
    conn.execute(f'SET ROLE "{store.role_prefix}_owner"')
    return conn


with SessionLocal() as db:
    user = db.scalar(select(User).where(User.github_id == -2600))
    store = db.scalar(select(DataStore).where(DataStore.owner_user_id == user.id))
    with as_owner(store) as conn:
        conn.execute(
            "CREATE TABLE data.fish (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,"
            " name text NOT NULL, weight_kg numeric)"
        )
        conn.execute("COMMENT ON TABLE data.fish IS 'Fish the user has caught'")
        conn.execute("COMMENT ON COLUMN data.fish.weight_kg IS 'As weighed on the boat'")
        conn.execute(
            "INSERT INTO data.fish (name, weight_kg) VALUES ('pike', 4.2), ('perch', 0.8),"
            " ('carp', 7.5)"
        )

    other = db.scalar(select(User).where(User.github_id == -2601))
    if other is None:
        other = User(github_id=-2601, login="ds26-other")
        db.add(other)
        db.commit()
    other_id = data_store_for(other.id)
    provision.provision(other_id)
    db.expire_all()
    other_store = db.get(DataStore, other_id)
    with as_owner(other_store) as conn:
        conn.execute("CREATE TABLE data.secrets (code text)")
        conn.execute("INSERT INTO data.secrets VALUES ('hunter2')")
    print(other_store.dbname)

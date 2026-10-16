"""Print the check user's exported history, read as their store's read-only role (as
`query_data` reads it). Usage: python store.py <label>   (run from backend/)"""

import sys

from sqlalchemy import select, text

from aios.datastores import store_session
from aios.db import SessionLocal
from aios.models import DataStore, User

with SessionLocal() as db:
    store_id = db.scalar(
        select(DataStore.id).join(User, User.id == DataStore.owner_user_id).where(
            User.github_id == -3400
        )
    )
print(f"== {sys.argv[1]} (store {store_id}, as its ro role)")
with store_session(store_id, "ro") as store:
    print("select current_user ->", store.scalar(text("SELECT current_user")))
    for sql in (
        "SELECT id, agent_name, agent_version, title, user_id, message_count FROM history.agent_runs ORDER BY id",
        "SELECT agent_run_id, position, role, text FROM history.messages ORDER BY 1, 2",
    ):
        print(sql)
        for row in store.execute(text(sql)):
            print("  ", tuple(row))

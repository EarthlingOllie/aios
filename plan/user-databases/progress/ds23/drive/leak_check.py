"""Are the browser check's store passwords anywhere in the Postgres server log (log_statement =
all) or the API's and worker's logs? Prints what each log holds of the role's ALTER ROLE."""

import sys
from pathlib import Path

from sqlalchemy import select

from aios.datastores import credentials
from aios.db import SessionLocal
from aios.models import DataStore, User

logs = [Path(p) for p in sys.argv[1:]]
with SessionLocal() as db:
    store = db.scalar(
        select(DataStore).join(User, User.id == DataStore.owner_user_id).where(User.github_id == -2300)
    )
passwords = [credentials.decrypt(store.rw_password), credentials.decrypt(store.ro_password)]
for log in logs:
    text = log.read_text(errors="replace")
    leaks = sum(text.count(p) for p in passwords)
    print(f"{log}: {len(text)} bytes, plaintext passwords found: {leaks}")
    for line in text.splitlines():
        if "ALTER ROLE" in line and store.role_prefix in line and "PASSWORD" in line:
            print("   ", line[:60], "...", line[line.index("PASSWORD") : line.index("PASSWORD") + 40])
            break

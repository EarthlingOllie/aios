import sys

sys.path.insert(0, "tests")
from conftest import delete_user  # noqa: E402
from sqlalchemy import select  # noqa: E402

from aios.db import SessionLocal  # noqa: E402
from aios.models import User  # noqa: E402

with SessionLocal() as db:
    for u in db.scalars(select(User)).all():
        print("deleting", u.login)
        delete_user(db, u)

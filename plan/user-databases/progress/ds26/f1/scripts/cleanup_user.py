"""Deletes the test user a killed run left behind, as the `user` fixture's teardown would."""

import sys

sys.path.insert(0, "tests")
from conftest import delete_user  # noqa: E402

from aios.db import SessionLocal  # noqa: E402
from aios.models import User  # noqa: E402

with SessionLocal() as db:
    for u in db.query(User).filter_by(github_id=-1).all():
        delete_user(db, u)
print("ok")

"""Delete the browser check's users (github ids -2600, -2601), and drop their stores."""

from sqlalchemy import select

from aios.datastores import provision
from aios.db import SessionLocal
from aios.models import DataStore, User

with SessionLocal() as db:
    for github_id in (-2600, -2601):
        user = db.scalar(select(User).where(User.github_id == github_id))
        if user is None:
            continue
        stores = [
            provision.StoreNames.of(s)
            for s in db.scalars(select(DataStore).where(DataStore.owner_user_id == user.id))
        ]
        db.delete(user)
        db.commit()
        for names in stores:
            provision.drop(names)
        print(f"reset {github_id}: dropped {len(stores)} store(s)")

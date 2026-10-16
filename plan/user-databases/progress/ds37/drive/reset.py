"""Delete the browser check's users (github ids -3700 and -3701), and drop their stores."""

from sqlalchemy import select

from aios.datastores import provision
from aios.db import SessionLocal
from aios.models import DataStore, User

with SessionLocal() as db:
    for user in db.scalars(select(User).where(User.github_id.in_([-3700, -3701]))).all():
        stores = [
            provision.StoreNames.of(s)
            for s in db.scalars(select(DataStore).where(DataStore.owner_user_id == user.id))
        ]
        db.delete(user)
        db.commit()
        for names in stores:
            provision.drop(names)
        print(f"reset: {user.login}, dropped {len(stores)} store(s)")

"""Delete the browser check's user (github id -3400), and drop their store's database."""

from sqlalchemy import select

from aios.datastores import provision
from aios.db import SessionLocal
from aios.models import DataStore, User

with SessionLocal() as db:
    user = db.scalar(select(User).where(User.github_id == -3400))
    if user is not None:
        stores = [
            provision.StoreNames.of(s)
            for s in db.scalars(select(DataStore).where(DataStore.owner_user_id == user.id))
        ]
        db.delete(user)
        db.commit()
        for names in stores:
            provision.drop(names)
        print(f"reset: dropped {len(stores)} store(s)")

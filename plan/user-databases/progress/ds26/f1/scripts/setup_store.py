"""Makes a user with a provisioned store for the memory measurement; prints the store id."""

from aios.datastores import data_store_for, provision
from aios.db import SessionLocal
from aios.models import User

with SessionLocal() as db:
    u = db.query(User).filter_by(github_id=-9001).one_or_none()
    if u is None:
        u = User(github_id=-9001, login="memory-measure")
        db.add(u)
        db.commit()
    store_id = data_store_for(u.id)
    provision.provision(store_id)
    print(store_id)

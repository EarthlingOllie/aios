"""What happens when an Engine is used after dispose() and then dropped (as search.py would)."""

import gc

from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

URL = "postgresql+psycopg://aios:aios@localhost:5498/aios_d8"
mon = create_engine(URL, poolclass=NullPool)


def count():
    with mon.connect() as c:
        return c.scalar(
            text("select count(*) from pg_stat_activity where application_name='probe'")
        )


gc.disable()
e = create_engine(URL, pool_size=1, max_overflow=2, connect_args={"application_name": "probe"})
old_pool = e.pool
e.dispose()  # LRU eviction while search awaits embed_query
print("same engine, new pool:", e.pool is not old_pool)
with e.connect() as c:
    c.execute(text("select 1"))
print("after use, connections:", count())
c = old_pool = None
e = None  # coroutine returns: last reference gone
print("after dropping engine (no gc):", count())
gc.collect()
import time
time.sleep(0.5)
print("after gc.collect() + 0.5s:", count())
gc.collect()
time.sleep(1)
print("after 2nd gc.collect() + 1s:", count())

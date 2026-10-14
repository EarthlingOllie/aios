from collections.abc import Iterator
from typing import Annotated

from annotated_types import Interval
from fastapi import Depends
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from aios.config import get_settings

engine = create_engine(get_settings().database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(engine, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    with SessionLocal() as session:
        yield session


# `scope="function"` closes the session when the endpoint returns, before the response is sent.
# The default ("request") would keep it, and its pooled connection, checked out until a
# streaming response finishes, so a few long agent chats could exhaust the pool.
DB = Annotated[Session, Depends(get_db, scope="function")]

# An id or version number from a request, as a path parameter or in a body. These columns are
# Postgres INTEGERs, and a number outside their range fails the query (a 500) instead of matching
# nothing, so it's rejected up front as invalid (422), like a non-numeric id. One in range that
# matches nothing is still a 404, or a 422 for an id in a body.
DbId = Annotated[int, Interval(ge=-(2**31), le=2**31 - 1)]

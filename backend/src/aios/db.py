from collections.abc import Iterator
from typing import Annotated

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

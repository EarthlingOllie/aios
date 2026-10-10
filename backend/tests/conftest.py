import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from aios.auth import current_user
from aios.db import SessionLocal, engine
from aios.main import app
from aios.models import User


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def db():
    """A session against a migrated database (`alembic upgrade head`); skips if unavailable."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1 FROM chunks LIMIT 1"))
    except Exception as exc:  # noqa: BLE001
        if isinstance(exc, OperationalError) or "chunks" in str(exc):
            pytest.skip(f"database not available: {exc.__class__.__name__}")
        raise
    with SessionLocal() as session:
        yield session
        session.rollback()


@pytest.fixture
def user(db):
    u = User(github_id=-1, login="test-user")
    db.add(u)
    db.commit()
    yield u
    db.delete(u)
    db.commit()


@pytest.fixture
def api(client, user):
    """A client signed in as `user`."""
    app.dependency_overrides[current_user] = lambda: user
    yield client
    app.dependency_overrides.clear()

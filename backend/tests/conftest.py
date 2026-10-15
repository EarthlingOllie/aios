import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from aios.auth import current_user
from aios.db import SessionLocal, engine
from aios.main import app
from aios.models import User
from aios.sandbox import runner


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
        # The server is up but won't let DATABASE_URL's role in: the roles haven't been created
        # (or the password is wrong), which skipping would hide.
        message = str(exc)
        if isinstance(exc, OperationalError) and (
            "password authentication failed" in message
            or ('role "' in message and "does not exist" in message)
        ):
            pytest.fail(
                f"DATABASE_URL's role can't log in ({exc.orig}). Create the roles with "
                "`python -m aios.db_roles` (see the README's Database roles)."
            )
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


@pytest.fixture
def workdir(monkeypatch):
    # Not under pytest's tmp_path, whose parents only its owner can enter: run as root, the
    # runner runs the code as another user, which needs to reach the working directory.
    with tempfile.TemporaryDirectory() as parent:
        os.chmod(parent, 0o755)
        work = Path(parent) / "work"
        monkeypatch.setattr(runner, "WORKDIR", str(work))
        runner.prepare_workdir()
        yield work

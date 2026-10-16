import sys
from fastapi.testclient import TestClient
from aios.main import app
from aios.auth import current_user
from aios.db import SessionLocal
from aios.models import User
with SessionLocal() as db:
    u = db.get(User, 1); db.expunge(u)
app.dependency_overrides[current_user] = lambda: u
c = TestClient(app)
for path in ["/api/data-store", "/api/documents"]:
    r = c.get(path); print("GET", path, r.status_code, r.text)
r = c.delete("/api/documents/1"); print("DELETE /api/documents/1", r.status_code, r.text)

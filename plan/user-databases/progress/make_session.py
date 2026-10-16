"""Run inside the api container: a user (as the app's role) and a session cookie for it, signed
the way Starlette's SessionMiddleware signs one, with the API's own SESSION_SECRET."""

import json
from base64 import b64encode

from itsdangerous import TimestampSigner
from sqlalchemy import select, text

from aios.config import get_settings
from aios.db import SessionLocal
from aios.models import User

with SessionLocal() as db:
    user = db.scalar(select(User).where(User.github_id == -2222))
    if user is None:
        user = User(github_id=-2222, login="ds22-e2e")
        db.add(user)
        db.commit()
    role = db.execute(text("SELECT current_user")).scalar()

settings = get_settings()
data = b64encode(json.dumps({"user_id": user.id}).encode())
cookie = TimestampSigner(settings.session_secret.get_secret_value()).sign(data).decode()
print(json.dumps({"cookie_name": settings.session_cookie, "cookie": cookie, "role": role}))

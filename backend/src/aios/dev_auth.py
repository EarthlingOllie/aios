"""The development "sign in as" picker: list every user, and sign in as any of them with no
password. `create_app` mounts this router only when `DEV_LOGIN_ENABLED` is on, so otherwise its
routes don't exist."""

import logging

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select

from aios.auth import MeOut, me_out, start_session
from aios.db import DB
from aios.models import User

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/dev")


@router.get("/users", response_model=list[MeOut])
def list_users(db: DB):
    """Every user, by last name, then first name."""
    rows = db.scalars(select(User).order_by(User.last_name, User.first_name, User.id))
    return [me_out(u) for u in rows]


class DevSessionIn(BaseModel):
    user_id: int


@router.post("/session", response_model=MeOut)
def sign_in_as(body: DevSessionIn, request: Request, db: DB):
    """Sign in as the user, as a password sign-in would (their data store queued if missing)."""
    user = db.get(User, body.user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such user")
    log.warning("Dev sign-in as user %s (%s)", user.id, user.email)
    start_session(request, user)
    return me_out(user)

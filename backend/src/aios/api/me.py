"""The signed-in user's own account, and which organisations they're in (`aios.organisations`)."""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ValidationInfo, field_validator
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from aios import ratelimits
from aios.auth import (
    EMAIL_TAKEN,
    NOT_SIGNED_IN,
    PASSWORD_MAX_LENGTH,
    CurrentUser,
    Email,
    MeOut,
    Name,
    NewPassword,
    check_confirmation,
    me_out,
    same_origin,
    start_session,
)
from aios.config import get_settings
from aios.db import DB
from aios.models import User
from aios.passwords import hash_password, verify_password

# Its PATCH and PUTs refuse cross-site requests.
router = APIRouter(prefix="/api/me", dependencies=[Depends(same_origin)])


@router.get("", response_model=MeOut)
def me(user: CurrentUser, db: DB):
    return me_out(db, user)


class NameIn(BaseModel):
    """A name edit, with registration's rules (`Name`). A name left out is kept; pydantic doesn't
    validate defaults, so the `None` only marks it unset, and an explicit null is refused."""

    first_name: Name = None
    last_name: Name = None


@router.patch("", response_model=MeOut)
def edit_name(body: NameIn, user: CurrentUser, db: DB):
    """Change the user's first and/or last name. No password is needed."""
    for field in body.model_fields_set:
        setattr(user, field, getattr(body, field))
    db.commit()
    return me_out(db, user)


def check_current_password(user: User, password: str) -> None:
    """Refuses (401) a request whose `password` isn't `user`'s current one. The checks are
    throttled per user (429 past the limit), so a stolen session can't be used to guess it. If
    the stored hash is due an upgrade, the user is given the new one (for the caller to commit)."""
    ratelimits.throttle(
        "current-password", get_settings().current_password_rate_limit_per_user, str(user.id)
    )
    ok, new_hash = (
        verify_password(password, user.password_hash)
        if len(password) <= PASSWORD_MAX_LENGTH
        # No account has one this long, and hashing it would be costly.
        else (False, None)
    )
    if not ok:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Current password is incorrect")
    if new_hash is not None:
        user.password_hash = new_hash


class EmailIn(BaseModel):
    email: Email
    current_password: str


@router.put("/email", response_model=MeOut)
def change_email(body: EmailIn, user: CurrentUser, db: DB):
    """Change the email the user signs in with, straight away (nothing is sent to it). It needs
    their current password. Their sessions are kept."""
    check_current_password(user, body.current_password)
    taken = select(User.id).where(User.email == body.email, User.id != user.id)
    if db.scalar(taken) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, EMAIL_TAKEN)
    user.email = body.email
    try:
        db.commit()
    except IntegrityError as exc:
        # Taken by another request since the check.
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, EMAIL_TAKEN) from exc
    return me_out(db, user)


class PasswordIn(BaseModel):
    current_password: str
    new_password: NewPassword
    new_password_confirm: str

    @field_validator("new_password_confirm")
    @classmethod
    def _confirms(cls, value: str, info: ValidationInfo) -> str:
        return check_confirmation(value, info, of="new_password")


@router.put("/password", status_code=status.HTTP_204_NO_CONTENT)
def change_password(body: PasswordIn, request: Request, user: CurrentUser, db: DB) -> None:
    """Change the user's password, given their current one. Every other session they have is
    signed out (its `session_version` is out of date); this one starts again, so it's kept."""
    check_current_password(user, body.current_password)
    # Only from the version this session holds: of two changes at once (from two sessions), the
    # second finds it bumped, so it's refused rather than both sessions surviving.
    bumped = db.execute(
        update(User)
        .where(User.id == user.id, User.session_version == user.session_version)
        .values(
            password_hash=hash_password(body.new_password),
            session_version=User.session_version + 1,
        )
    )
    if bumped.rowcount != 1:
        db.rollback()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, NOT_SIGNED_IN)
    db.commit()
    db.refresh(user)
    start_session(request, db, user)

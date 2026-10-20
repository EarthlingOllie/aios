"""The signed-in user's own account."""

from fastapi import APIRouter
from pydantic import BaseModel

from aios.auth import CurrentUser, MeOut, Name, me_out
from aios.db import DB

router = APIRouter(prefix="/api/me")


@router.get("", response_model=MeOut)
def me(user: CurrentUser):
    return me_out(user)


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
    return me_out(user)

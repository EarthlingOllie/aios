"""The signed-in user's own account."""

from fastapi import APIRouter

from aios.auth import CurrentUser, MeOut, me_out

router = APIRouter(prefix="/api/me")


@router.get("", response_model=MeOut)
def me(user: CurrentUser):
    return me_out(user)

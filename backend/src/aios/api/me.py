"""The signed-in user's own account."""

from fastapi import APIRouter, Depends

from aios.auth import CurrentUser, MeOut, me_out, same_origin

# Its PATCH and PUTs refuse cross-site requests.
router = APIRouter(prefix="/api/me", dependencies=[Depends(same_origin)])


@router.get("", response_model=MeOut)
def me(user: CurrentUser):
    return me_out(user)

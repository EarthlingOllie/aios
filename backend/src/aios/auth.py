"""GitHub OAuth (Authlib) and cookie sessions (Starlette SessionMiddleware).

Authlib keeps the OAuth `state` in `request.session`, and after the callback we store
only the user's id there. The session is a signed (itsdangerous) cookie, so the SPA
never handles tokens; it just sends the cookie.

Signing in from an invitation link's page (`/login?invite=<token>`) keeps the token in the
session through the round trip, and the callback joins a new user to the link's organisation
instead of making them a personal one (`aios.organisations.ensure_membership`). A user it
couldn't join (the link no longer opens, or they're in an organisation already) is sent back
to the link's page, which says why.
"""

import uuid
from typing import Annotated
from urllib.parse import quote

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from aios import organisations
from aios.config import get_settings
from aios.datastores import data_store_for, data_store_for_request
from aios.db import DB
from aios.models import Organisation, User

settings = get_settings()

oauth = OAuth()
oauth.register(
    name="github",
    client_id=settings.github_client_id,
    client_secret=settings.github_client_secret.get_secret_value(),
    access_token_url="https://github.com/login/oauth/access_token",
    authorize_url="https://github.com/login/oauth/authorize",
    api_base_url="https://api.github.com/",
    client_kwargs={"scope": "read:user user:email"},
)

router = APIRouter()


# Where the session keeps the invitation link's token through the OAuth round trip.
INVITE_TOKEN = "invite_token"


@router.get("/login")
async def login(request: Request, invite: str | None = None):
    if invite:
        request.session[INVITE_TOKEN] = invite
    else:
        request.session.pop(INVITE_TOKEN, None)
    return await oauth.github.authorize_redirect(request, settings.oauth_redirect_uri)


@router.get("/auth/callback")
async def auth_callback(request: Request, db: DB):
    try:
        token = await oauth.github.authorize_access_token(request)
    except OAuthError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, exc.description or exc.error) from exc

    profile = (await oauth.github.get("user", token=token)).json()
    email = profile.get("email")
    if not email:
        emails = (await oauth.github.get("user/emails", token=token)).json()
        email = next((e["email"] for e in emails if e.get("primary") and e.get("verified")), None)

    user = db.scalar(select(User).where(User.github_id == profile["id"]))
    if user is None:
        user = User(github_id=profile["id"])
        db.add(user)
    user.login = profile["login"]
    user.name = profile.get("name")
    user.email = email
    user.avatar_url = profile.get("avatar_url")
    db.flush()
    # Their first sign-in makes their personal organisation, or joins the one they were invited
    # to; afterwards, it's found again.
    invite_token = request.session.get(INVITE_TOKEN)
    invite = organisations.open_invite(db, invite_token, lock=True) if invite_token else None
    org = organisations.ensure_membership(db, user, invite)
    db.commit()
    await run_in_threadpool(data_store_for, user.id)

    landing = settings.public_url
    if invite_token and (invite is None or org.id != invite.organisation_id):
        landing = f"{settings.public_url.rstrip('/')}/invite/{quote(invite_token, safe='')}"
    # Drop anything left over (e.g. OAuth state) and start a fresh session.
    request.session.clear()
    request.session["user_id"] = user.id
    return RedirectResponse(landing, status_code=status.HTTP_303_SEE_OTHER)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request) -> None:
    request.session.clear()


def current_user(request: Request, db: DB) -> User:
    user_id = request.session.get("user_id")
    user = db.get(User, user_id) if user_id is not None else None
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not signed in")
    return user


CurrentUser = Annotated[User, Depends(current_user)]


def current_organisation(user: CurrentUser, db: DB) -> Organisation:
    """The signed-in user's organisation: their one membership (`aios.organisations`). The one
    place a request's organisation is resolved, so when a user can be in several, this is where
    the request says which it means."""
    org = organisations.membership_of(db, user.id)
    if org is None:
        # Sign-in, leaving and the upgrade each put a user in one; this is a bug, not a request
        # that can be answered.
        raise RuntimeError(f"user {user.id} is in no organisation")
    return org


# For endpoints that scope what they touch to the organisation (the user, `CurrentUser`, is for
# provenance, notifications and chat membership).
CurrentOrganisation = Annotated[Organisation, Depends(current_organisation)]


def current_data_store(user: CurrentUser, db: DB) -> uuid.UUID:
    """The signed-in user's data store. The request's session is closed once it's resolved (see
    `data_store_for_request`), so an endpoint that takes this mustn't use `DB` after it."""
    return data_store_for_request(user.id, db)


# For endpoints that reach the user's data only through their store.
CurrentDataStore = Annotated[uuid.UUID, Depends(current_data_store)]

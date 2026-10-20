"""Accounts and cookie sessions: register and sign in with an email and password.

The session is Starlette's SessionMiddleware: a signed (itsdangerous) cookie holding the user's
id and their `session_version` as it was when it started, so the SPA never handles tokens; it
just sends the cookie. Bumping a user's `session_version` signs out every session they have.

Hashing a password takes tens of milliseconds of CPU, so the endpoints that do are sync: FastAPI
runs them in the threadpool.

Starting a session puts the user in their organisation (`aios.organisations.ensure_membership`):
registering makes them a personal one, or, from an invitation link's page (the link's token in
the request), joins them to the link's organisation instead. A link that no longer opens is
ignored, and the invite page they're sent back to says why.
"""

import uuid
from typing import Annotated
from urllib.parse import urlsplit

from email_validator import EmailNotValidError, validate_email
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import (
    AfterValidator,
    BaseModel,
    Field,
    StringConstraints,
    ValidationInfo,
    field_validator,
)
from pydantic_core import PydanticCustomError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from aios import organisations, ratelimits
from aios.config import get_settings
from aios.datastores import data_store_for, data_store_for_request
from aios.db import DB
from aios.models import Organisation, OrganisationInvite, User
from aios.passwords import hash_password, verify_password


def _origin(url: str) -> str:
    parts = urlsplit(url.strip())
    return f"{parts.scheme}://{parts.netloc}".lower()


def same_origin(request: Request) -> None:
    """Refuses (403) a request that changes something (POST, PUT, PATCH, DELETE) sent by a page
    on another site: its `Origin` isn't `PUBLIC_URL`'s. Browsers send `Origin` with every such
    request, so one without it isn't from a browser, and is let through. A second guard to
    the SameSite cookie, and to these endpoints only taking JSON (which a cross-site form can't
    send). For the auth and profile routers."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    origin = request.headers.get("origin")
    if origin is not None and _origin(origin) != _origin(get_settings().public_url):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Cross-site request refused")


router = APIRouter(prefix="/api/auth", dependencies=[Depends(same_origin)])

PASSWORD_MIN_LENGTH = 12
# Hashing a very long password is costly, so a longer one is refused rather than hashed.
PASSWORD_MAX_LENGTH = 256
# The longest invitation link token a registration takes (a real one is 43 characters).
INVITE_TOKEN_MAX = 256


def normalise_email(email: str) -> str:
    """`email` as it's stored and looked up: trimmed, email-validator's normalised form (no DNS
    check, so it works offline), then lower-cased, so one address in any case is one account.
    Raises a validation error (422) when it isn't an email address."""
    try:
        normalised = validate_email(email.strip(), check_deliverability=False).normalized
    except EmailNotValidError as exc:
        raise PydanticCustomError("email", "{reason}", {"reason": str(exc)}) from exc
    return normalised.lower()


def check_new_password(password: str) -> str:
    """A password someone is choosing: 12 to 256 characters, with no rules about what's in it."""
    if len(password) < PASSWORD_MIN_LENGTH:
        raise PydanticCustomError(
            "password_too_short",
            "Password must be at least {min} characters",
            {"min": PASSWORD_MIN_LENGTH},
        )
    if len(password) > PASSWORD_MAX_LENGTH:
        raise PydanticCustomError(
            "password_too_long",
            "Password must be at most {max} characters",
            {"max": PASSWORD_MAX_LENGTH},
        )
    return password


def check_confirmation(confirmation: str, info: ValidationInfo, of: str) -> str:
    """For a model's confirmation field: that it repeats the field `of` (unless that was refused
    already, which says why)."""
    if of in info.data and confirmation != info.data[of]:
        raise PydanticCustomError("password_mismatch", "Passwords don't match")
    return confirmation


# Request fields shared by everything that takes a name, an email or a new password.
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Email = Annotated[str, AfterValidator(normalise_email)]
NewPassword = Annotated[str, AfterValidator(check_new_password)]


class AccountOut(BaseModel):
    """A user's account details, as the dev picker lists them."""

    id: int
    first_name: str
    last_name: str
    email: str


def account_out(user: User) -> AccountOut:
    return AccountOut(
        id=user.id, first_name=user.first_name, last_name=user.last_name, email=user.email
    )


class OrganisationSummaryOut(BaseModel):
    id: int
    name: str
    # Made for one user on sign-up (or on leaving another organisation).
    personal: bool
    # Whether the signed-in user is its creator, who alone may rename it.
    is_creator: bool


def summary_out(org: Organisation, user: User) -> OrganisationSummaryOut:
    return OrganisationSummaryOut(
        id=org.id, name=org.name, personal=org.personal, is_creator=org.created_by == user.id
    )


class MeOut(AccountOut):
    """The signed-in user and the organisation they're in, as `GET /api/me` and signing in
    answer (the shell shows both)."""

    organisation: OrganisationSummaryOut


def me_out(db: Session, user: User) -> MeOut:
    org = organisations.membership_of(db, user.id)
    if org is None:
        # Starting a session and leaving each put a user in one (see `current_organisation`).
        raise RuntimeError(f"user {user.id} is in no organisation")
    return MeOut(**account_out(user).model_dump(), organisation=summary_out(org, user))


def start_session(
    request: Request, db: Session, user: User, invite: OrganisationInvite | None = None
) -> Organisation:
    """Sign `user` in on this request: the one way a session starts. They're put in their
    organisation if they're in none (`organisations.ensure_membership`: the open invitation
    link's `invite`, else a new personal one), whose data store is made (and queued to be
    provisioned) if it's missing, and `db` is committed. Then anything left in the session is
    dropped, and the fresh one holds their id and current `session_version`. It blocks (on the
    database), so call it from a sync endpoint. Answers the user's organisation."""
    org = organisations.ensure_membership(db, user, invite)
    data_store_for(org.id, db)
    db.commit()
    request.session.clear()
    request.session["user_id"] = user.id
    request.session["session_version"] = user.session_version
    return org


# A request's session has ended (or never started). The SPA tells it apart from other 401s (a
# wrong password) by this detail, and sends the user to sign in.
NOT_SIGNED_IN = "Not signed in"


def current_user(request: Request, db: DB) -> User:
    user_id = request.session.get("user_id")
    user = db.get(User, user_id) if user_id is not None else None
    # A session from before the user's `session_version` was bumped is signed out.
    if user is None or request.session.get("session_version") != user.session_version:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, NOT_SIGNED_IN)
    return user


CurrentUser = Annotated[User, Depends(current_user)]


def current_organisation(user: CurrentUser, db: DB) -> Organisation:
    """The signed-in user's organisation: their one membership (`aios.organisations`). The one
    place a request's organisation is resolved, so when a user can be in several, this is where
    the request says which it means."""
    org = organisations.membership_of(db, user.id)
    if org is None:
        # Signing in, leaving and the upgrade each put a user in one; this is a bug, not a
        # request that can be answered.
        raise RuntimeError(f"user {user.id} is in no organisation")
    return org


# For endpoints that scope what they touch to the organisation (the user, `CurrentUser`, is for
# provenance, notifications and chat membership).
CurrentOrganisation = Annotated[Organisation, Depends(current_organisation)]


def current_data_store(org: CurrentOrganisation, db: DB) -> uuid.UUID:
    """The signed-in user's organisation's data store, the one every member reaches. The
    request's session is closed once it's resolved (see `data_store_for_request`), so an
    endpoint that takes this mustn't use `DB` after it."""
    return data_store_for_request(org.id, db)


# For endpoints that reach the organisation's data only through its store.
CurrentDataStore = Annotated[uuid.UUID, Depends(current_data_store)]


class OptionsOut(BaseModel):
    # Whether the sign-in page offers the development "sign in as" picker.
    dev_users: bool


@router.get("/options", response_model=OptionsOut)
def options():
    return OptionsOut(dev_users=get_settings().dev_login_enabled)


class RegisterIn(BaseModel):
    first_name: Name
    last_name: Name
    email: Email
    password: NewPassword
    password_confirm: str
    # The token of the invitation link whose page they came from, if any: they join its
    # organisation instead of getting a personal one, if it still opens.
    invite: str | None = Field(default=None, max_length=INVITE_TOKEN_MAX)

    @field_validator("password_confirm")
    @classmethod
    def _confirms(cls, value: str, info: ValidationInfo) -> str:
        return check_confirmation(value, info, of="password")


EMAIL_TAKEN = "An account with this email already exists"


@router.post("/register", response_model=MeOut, status_code=status.HTTP_201_CREATED)
def register(body: RegisterIn, request: Request, db: DB):
    """A new account, signed in, in a new personal organisation (or the one the invitation link
    `invite` is for, if it still opens), with its data store queued to be provisioned."""
    settings = get_settings()
    ratelimits.throttle(
        "register-ip", settings.register_rate_limit_per_ip, ratelimits.client_ip(request)
    )
    if db.scalar(select(User.id).where(User.email == body.email)) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, EMAIL_TAKEN)
    user = User(
        first_name=body.first_name,
        last_name=body.last_name,
        email=body.email,
        password_hash=hash_password(body.password),
    )
    db.add(user)
    try:
        db.flush()
    except IntegrityError as exc:
        # Registered by another request since the check.
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, EMAIL_TAKEN) from exc
    invite = organisations.open_invite(db, body.invite, lock=True) if body.invite else None
    start_session(request, db, user, invite)
    return me_out(db, user)


class LoginIn(BaseModel):
    email: str
    password: str


BAD_CREDENTIALS = "Email or password is incorrect"


@router.post("/login", response_model=MeOut)
def login(body: LoginIn, request: Request, db: DB):
    """Sign in. An unknown email and a wrong password get the same answer, in the same time.
    Attempts are throttled per IP (across emails), then per email (across IPs)."""
    try:
        email = normalise_email(body.email)
    except PydanticCustomError:
        email = None
    settings = get_settings()
    ratelimits.throttle("login-ip", settings.login_rate_limit_per_ip, ratelimits.client_ip(request))
    ratelimits.throttle(
        "login-email",
        settings.login_rate_limit_per_email,
        ratelimits.hashed(email or body.email.strip().lower()),
    )
    if len(body.password) > PASSWORD_MAX_LENGTH:
        # No account has one this long, and hashing it would be costly.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, BAD_CREDENTIALS)
    user = db.scalar(select(User).where(User.email == email)) if email else None
    ok, new_hash = verify_password(body.password, user.password_hash if user else None)
    if user is None or not ok:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, BAD_CREDENTIALS)
    if new_hash is not None:
        user.password_hash = new_hash
    start_session(request, db, user)
    return me_out(db, user)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request) -> None:
    request.session.clear()

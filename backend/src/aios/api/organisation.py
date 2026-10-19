"""The signed-in user (`GET /api/me`) and their organisation (`/api/organisation`, see
`aios.organisations`).

`/api/me` says who the user is and which organisation they're in, which the shell shows.
`GET /api/organisation` is the Organisation page: the name, the creator and the members with
when each joined; `PUT` renames it, for the creator only (a 403 for another member), to a name
no other organisation has (a 409 otherwise).

Invitation links (`/api/organisation/invites`) are the creator's too: making one (its token
shown in the response, once), listing the open ones and revoking one are a 403 for any other
member. `/api/invites/{token}` is the invite page: what the link is for, with no sign-in
needed, and accepting it (`/accept`, 409 with the reason when the user can't). An expired or
revoked link is a 404. `POST /api/organisation/leave` leaves (409 with the reason when the user
can't) and answers with the new personal organisation the user lands in.
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from aios import organisations
from aios.api.skills import on_conflict
from aios.auth import CurrentOrganisation, CurrentUser
from aios.config import get_settings
from aios.db import DB
from aios.models import Organisation, OrganisationInvite, OrganisationMember, User

router = APIRouter(prefix="/api")

NAME_CONSTRAINT = "organisations_name_key"
NAME_TAKEN = "An organisation with that name already exists."
NOT_THE_CREATOR = "Only the organisation's creator can rename it."
NOT_THE_INVITER = "Only the organisation's creator can invite people into it."
NO_SUCH_INVITE = "This invitation link has expired or been revoked, or never existed."
# The one-organisation-per-user index: two accepts at once by one user.
MEMBERSHIP_CONSTRAINT = "uq_organisation_members_user_id"


class UserOut(BaseModel):
    id: int
    login: str
    name: str | None
    avatar_url: str | None


class OrganisationSummaryOut(BaseModel):
    id: int
    name: str
    # Made for one user on sign-up (or on leaving another organisation).
    personal: bool
    # Whether the signed-in user is its creator, who alone may rename it.
    is_creator: bool


class MeOut(UserOut):
    organisation: OrganisationSummaryOut


class MemberOut(BaseModel):
    user: UserOut
    joined_at: datetime
    # The invitation link they came in through, if any.
    invite_id: uuid.UUID | None


class OrganisationOut(OrganisationSummaryOut):
    # Null once the creator's account is deleted.
    creator: UserOut | None
    # In the order they joined.
    members: list[MemberOut]
    created_at: datetime


class OrganisationIn(BaseModel):
    name: str = Field(min_length=1, max_length=255, pattern=r"\S")


def summary_out(org: Organisation, user: User) -> OrganisationSummaryOut:
    return OrganisationSummaryOut(
        id=org.id, name=org.name, personal=org.personal, is_creator=org.created_by == user.id
    )


def organisation_out(db: Session, org: Organisation, user: User) -> OrganisationOut:
    members = db.scalars(
        select(OrganisationMember)
        .options(selectinload(OrganisationMember.user))
        .where(OrganisationMember.organisation_id == org.id)
        .order_by(OrganisationMember.joined_at, OrganisationMember.user_id)
    ).all()
    return OrganisationOut(
        **summary_out(org, user).model_dump(),
        creator=UserOut.model_validate(org.creator, from_attributes=True) if org.creator else None,
        members=[
            MemberOut(
                user=UserOut.model_validate(m.user, from_attributes=True),
                joined_at=m.joined_at,
                invite_id=m.invite_id,
            )
            for m in members
        ],
        created_at=org.created_at,
    )


@router.get("/me", response_model=MeOut)
def me(user: CurrentUser, org: CurrentOrganisation):
    return MeOut(
        id=user.id,
        login=user.login,
        name=user.name,
        avatar_url=user.avatar_url,
        organisation=summary_out(org, user),
    )


@router.get("/organisation", response_model=OrganisationOut)
def get_organisation(user: CurrentUser, org: CurrentOrganisation, db: DB):
    return organisation_out(db, org, user)


@router.put("/organisation", response_model=OrganisationOut)
def rename_organisation(body: OrganisationIn, user: CurrentUser, org: CurrentOrganisation, db: DB):
    if org.created_by != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, NOT_THE_CREATOR)
    org.name = body.name.strip()
    with on_conflict(db, NAME_CONSTRAINT, NAME_TAKEN):
        db.commit()
    return organisation_out(db, org, user)


# Invitation links (`aios.organisations`).


class InviteIn(BaseModel):
    expires_in_days: int = Field(organisations.INVITE_DAYS, ge=1, le=organisations.INVITE_DAYS_MAX)


class InviteOut(BaseModel):
    id: uuid.UUID
    # Null once their account is deleted.
    creator: UserOut | None
    created_at: datetime
    expires_at: datetime


class NewInviteOut(InviteOut):
    # The secret in the link: in this response only, since only its hash is kept.
    token: str
    url: str


class InvitedToOut(BaseModel):
    id: int
    name: str


class InvitationOut(BaseModel):
    """What the invite page shows of an open link, to anyone holding it."""

    organisation: InvitedToOut
    expires_at: datetime


def invite_out(invite: OrganisationInvite, db: Session) -> InviteOut:
    creator = db.get(User, invite.created_by) if invite.created_by is not None else None
    return InviteOut(
        id=invite.id,
        creator=UserOut.model_validate(creator, from_attributes=True) if creator else None,
        created_at=invite.created_at,
        expires_at=invite.expires_at,
    )


def require_creator(org: Organisation, user: User) -> None:
    if org.created_by != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, NOT_THE_INVITER)


@router.post(
    "/organisation/invites", response_model=NewInviteOut, status_code=status.HTTP_201_CREATED
)
def make_invite(body: InviteIn, user: CurrentUser, org: CurrentOrganisation, db: DB):
    require_creator(org, user)
    invite, token = organisations.make_invite(db, org, user, body.expires_in_days)
    db.commit()
    return NewInviteOut(
        **invite_out(invite, db).model_dump(),
        token=token,
        url=f"{get_settings().public_url.rstrip('/')}/invite/{token}",
    )


@router.get("/organisation/invites", response_model=list[InviteOut])
def list_invites(user: CurrentUser, org: CurrentOrganisation, db: DB):
    require_creator(org, user)
    return [invite_out(i, db) for i in organisations.open_invites(db, org)]


@router.delete("/organisation/invites/{invite_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_invite(invite_id: uuid.UUID, user: CurrentUser, org: CurrentOrganisation, db: DB):
    require_creator(org, user)
    invite = db.scalar(
        select(OrganisationInvite).where(
            OrganisationInvite.id == invite_id,
            OrganisationInvite.organisation_id == org.id,
            organisations.is_open(),
        )
    )
    if invite is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NO_SUCH_INVITE)
    invite.revoked_at = func.now()
    db.commit()


@router.get("/invites/{token}", response_model=InvitationOut)
def open_invite(token: str, db: DB):
    invite = organisations.open_invite(db, token)
    if invite is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NO_SUCH_INVITE)
    org = db.get_one(Organisation, invite.organisation_id)
    return InvitationOut(
        organisation=InvitedToOut(id=org.id, name=org.name), expires_at=invite.expires_at
    )


@router.post("/invites/{token}/accept", response_model=OrganisationOut)
def accept_invite(token: str, user: CurrentUser, db: DB):
    invite = organisations.open_invite(db, token, lock=True)
    if invite is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NO_SUCH_INVITE)
    if (refusal := organisations.accept_refusal(db, user, invite)) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, refusal)
    with on_conflict(db, MEMBERSHIP_CONSTRAINT, organisations.ONE_AT_A_TIME):
        org = organisations.join(db, user, invite)
        db.commit()
    return organisation_out(db, org, user)


@router.post("/organisation/leave", response_model=OrganisationOut)
def leave_organisation(user: CurrentUser, org: CurrentOrganisation, db: DB):
    if (refusal := organisations.leave_refusal(db, org, user)) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, refusal)
    personal = organisations.leave(db, org, user)
    db.commit()
    return organisation_out(db, personal, user)

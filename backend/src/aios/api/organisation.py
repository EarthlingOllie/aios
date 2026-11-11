"""Organisations (`aios.organisations`): creating one, and the organisation a request is in
(`/api/o/{organisation_id}/organisation`); which ones the signed-in person is in is on
`GET /api/me` (`aios.api.me`), which the shell's switcher lists.

`POST /api/organisations` creates an organisation from a name, with the person as its creator and
only member, and its store queued to be provisioned at once. Names may repeat: the slug tells
organisations apart. It's the welcome page's, and the switcher's "Create an organisation".

`GET .../organisation` is the Organisation page: the name, the creator, the members with when
each joined, and its deletion while one is scheduled; `PUT` renames it, for the creator only (a
403 for another member).

`POST .../organisation/deletion` schedules its deletion, for the creator only, who types its name
(a 422 for another name, a 409 if it's scheduled already); `DELETE` cancels it (a 409 if none
is scheduled). Both answer with the Organisation page (`aios.organisations`).

Invitation links (`.../organisation/invites`) are the creator's too: making one (its token
shown in the response, once), listing the open ones and revoking one are a 403 for any other
member. `/api/invites/{token}` is the invite page: what the link is for, with no sign-in
needed, and accepting it (`/accept`), which adds a membership and answers with the organisation,
the same again for one the person is in already. An expired or revoked link is a 404.
`POST .../organisation/leave` leaves (409 with the reason when the user can't): only the
membership goes, and it answers with the person as "me" does, with the organisations they're
still in.
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from aios import organisations
from aios.api.provenance import UserOut, people
from aios.auth import (
    CurrentOrganisation,
    CurrentUser,
    MeOut,
    OrganisationSummaryOut,
    me_out,
    summary_out,
)
from aios.config import get_settings
from aios.datastores import data_store_for
from aios.db import DB, SessionLocal
from aios.models import Organisation, OrganisationInvite, OrganisationMember, User

router = APIRouter(prefix="/api")

NOT_THE_CREATOR = "Only the organisation's creator can rename it."
NOT_THE_DELETER = "Only the organisation's creator can delete it."
NOT_THE_INVITER = "Only the organisation's creator can invite people into it."
NO_SUCH_INVITE = "This invitation link has expired or been revoked, or never existed."


class MemberOut(BaseModel):
    user: UserOut
    joined_at: datetime
    # The invitation link they came in through, if any.
    invite_id: uuid.UUID | None


class DeletionOut(BaseModel):
    """An organisation's scheduled deletion."""

    scheduled_at: datetime
    # Null once their account is deleted.
    scheduled_by: UserOut | None
    # When the grace period ends and it's deleted, unless the creator cancels first.
    deletes_at: datetime


class OrganisationOut(OrganisationSummaryOut):
    # Null once the creator's account is deleted.
    creator: UserOut | None
    # In the order they joined.
    members: list[MemberOut]
    created_at: datetime
    # Its deletion, while one is scheduled; else null.
    deletion: DeletionOut | None


class DeletionIn(BaseModel):
    # The organisation's name, typed to confirm.
    name: str


class OrganisationIn(BaseModel):
    name: str = Field(min_length=1, max_length=255, pattern=r"\S")


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
        deletion=deletion_out(db, org),
    )


def deletion_out(db: Session, org: Organisation) -> DeletionOut | None:
    if org.deletes_at is None:
        return None
    by = db.get(User, org.deletion_scheduled_by) if org.deletion_scheduled_by else None
    return DeletionOut(
        scheduled_at=org.deletion_scheduled_at,
        scheduled_by=UserOut.model_validate(by, from_attributes=True) if by else None,
        deletes_at=org.deletes_at,
    )


@router.post("/organisations", response_model=OrganisationOut, status_code=status.HTTP_201_CREATED)
def create_organisation(body: OrganisationIn, user: CurrentUser, db: DB):
    """A new organisation, with the signed-in person as its creator and only member, and its
    store queued to be provisioned once this commits."""
    org = organisations.create(db, user, body.name.strip())
    data_store_for(org.id, db)
    db.commit()
    return organisation_out(db, org, user)


@router.get("/organisation", response_model=OrganisationOut)
def get_organisation(user: CurrentUser, org: CurrentOrganisation, db: DB):
    return organisation_out(db, org, user)


@router.put("/organisation", response_model=OrganisationOut)
def rename_organisation(body: OrganisationIn, user: CurrentUser, org: CurrentOrganisation, db: DB):
    if org.created_by != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, NOT_THE_CREATOR)
    org.name = body.name.strip()
    db.commit()
    return organisation_out(db, org, user)


@router.post("/organisation/deletion", response_model=OrganisationOut)
def schedule_deletion(body: DeletionIn, user: CurrentUser, org: CurrentOrganisation, db: DB):
    if org.created_by != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, NOT_THE_DELETER)
    try:
        organisations.schedule_deletion(db, org, user, body.name.strip())
    except organisations.DeletionRefused as refused:
        db.rollback()
        code = (
            status.HTTP_422_UNPROCESSABLE_CONTENT
            if str(refused) == organisations.NAME_MISMATCH
            else status.HTTP_409_CONFLICT
        )
        raise HTTPException(code, str(refused)) from None
    db.commit()
    return organisation_out(db, org, user)


@router.delete("/organisation/deletion", response_model=OrganisationOut)
def cancel_deletion(user: CurrentUser, org: CurrentOrganisation, db: DB):
    if org.created_by != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, NOT_THE_DELETER)
    try:
        organisations.cancel_deletion(db, org)
    except organisations.DeletionRefused as refused:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(refused)) from None
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
    org = organisations.join(db, user, invite)
    db.commit()
    return organisation_out(db, org, user)


@router.post("/organisation/leave", response_model=MeOut)
def leave_organisation(user: CurrentUser, org: CurrentOrganisation, db: DB):
    if (refusal := organisations.leave_refusal(db, org, user)) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, refusal)
    organisations.leave(db, org, user)
    db.commit()
    return me_out(db, user)


def users_out(user_ids: set[int]) -> dict[int, UserOut]:
    """The users with these ids whose accounts are still there, by id, on a session of its own:
    for what records who did it where users can't be joined (a data store's documents)."""
    if not user_ids:
        return {}
    with SessionLocal() as db:
        return people(db, user_ids)

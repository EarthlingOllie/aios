"""Organisations (`aios.organisations`): creating one, and the organisation a request is in
(`/api/o/{organisation_id}/organisation`); which ones the signed-in person is in is on
`GET /api/me` (`aios.api.me`), which the shell's switcher lists.

`POST /api/organisations` creates an organisation from a name, with the person as its creator and
only member, and its store queued to be provisioned at once. Names may repeat: the slug tells
organisations apart. It's the welcome page's, and the switcher's "Create an organisation".

`GET .../organisation` is the Organisation page: the name, the creator, the members with their
roles and when each joined, the signed-in member's own role, and its deletion while one is
scheduled. What only admins do (`aios.permissions`) is a 403 naming it for anyone else:
renaming it (`PUT`), setting how long runs and chat history are kept (`PUT
.../organisation/retention`, `aios.retention`; every member sees it as `retention`), changing a
member's role (`PUT .../organisation/members/{user_id}/role`,
a 409 for demoting its last admin), and scheduling its deletion
(`POST .../organisation/deletion`, typing its name: a 422 for another name, a 409 if it's
scheduled already) or cancelling it (`DELETE`, a 409 if none is scheduled).

Invitation links (`.../organisation/invites`) are admins' too: making one (its token shown in
the response, once, and the role it grants, editor unless they choose), listing the open ones
and revoking one. `/api/invites/{token}` is the invite page: what the link is for, with no
sign-in needed, and accepting it (`/accept`), which adds a membership with the link's role and
answers with the organisation, the same again for one the person is in already. An expired or
revoked link is a 404.
`POST .../organisation/leave` leaves (409 with the reason when the user can't): only the
membership goes, and it answers with the person as "me" does, with the organisations they're
still in.
"""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from aios import audit, organisations
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
from aios.models import (
    DataStore,
    Organisation,
    OrganisationInvite,
    OrganisationMember,
    Role,
    User,
)
from aios.permissions import Action, allow

router = APIRouter(prefix="/api")

NO_SUCH_INVITE = "This invitation link has expired or been revoked, or never existed."


class MemberOut(BaseModel):
    user: UserOut
    role: Role
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


# Retention runs from a day to a hundred years (`aios.retention`).
RetentionDays = Annotated[int, Field(strict=True, ge=1, le=36_500)]


class RetentionOut(BaseModel):
    """How many days finished runs, and chats' messages, are kept: null keeps them
    indefinitely."""

    run_days: int | None
    chat_days: int | None


class RetentionIn(BaseModel):
    run_days: RetentionDays | None
    chat_days: RetentionDays | None


class OrganisationOut(OrganisationSummaryOut):
    # Null once the creator's account is deleted.
    creator: UserOut | None
    # In the order they joined.
    members: list[MemberOut]
    created_at: datetime
    # Its deletion, while one is scheduled; else null.
    deletion: DeletionOut | None
    # How many of its items the run service runs at once (`aios.run_service`): an admin's
    # override, else the deployment's default; and whether it's overridden.
    in_flight_cap: int
    in_flight_cap_overridden: bool
    # Every member sees it; admins change it.
    retention: RetentionOut


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
        **summary_out(db, org, user).model_dump(),
        creator=UserOut.model_validate(org.creator, from_attributes=True) if org.creator else None,
        members=[
            MemberOut(
                user=UserOut.model_validate(m.user, from_attributes=True),
                role=m.role,
                joined_at=m.joined_at,
                invite_id=m.invite_id,
            )
            for m in members
        ],
        created_at=org.created_at,
        deletion=deletion_out(db, org),
        in_flight_cap=org.in_flight_cap or get_settings().queue_in_flight_cap,
        in_flight_cap_overridden=org.in_flight_cap is not None,
        retention=retention_out(org),
    )


def retention_out(org: Organisation) -> RetentionOut:
    return RetentionOut(run_days=org.run_retention_days, chat_days=org.chat_retention_days)


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


@router.get("/organisation", response_model=OrganisationOut, dependencies=[allow(Action.read)])
def get_organisation(user: CurrentUser, org: CurrentOrganisation, db: DB):
    return organisation_out(db, org, user)


@router.put(
    "/organisation", response_model=OrganisationOut, dependencies=[allow(Action.run_organisation)]
)
def rename_organisation(body: OrganisationIn, user: CurrentUser, org: CurrentOrganisation, db: DB):
    old, new = org.name, body.name.strip()
    org.name = new
    if new != old:
        audit.enqueue(
            db,
            org.id,
            audit.person(user.id),
            "organisation.renamed",
            "organisation",
            org.id,
            {"from": old, "to": new},
        )
    db.commit()
    return organisation_out(db, org, user)


class InFlightCapIn(BaseModel):
    # Null goes back to the deployment's default.
    in_flight_cap: int | None = Field(ge=1, le=10_000)


@router.put(
    "/organisation/in-flight-cap",
    response_model=OrganisationOut,
    dependencies=[allow(Action.run_organisation)],
)
def set_in_flight_cap(body: InFlightCapIn, user: CurrentUser, org: CurrentOrganisation, db: DB):
    """Override how many of the organisation's items the run service runs at once (null: the
    default). Admins only."""
    old = org.in_flight_cap
    org.in_flight_cap = body.in_flight_cap
    if old != body.in_flight_cap:
        audit.enqueue(
            db,
            org.id,
            audit.person(user.id),
            "settings.changed",
            "organisation",
            org.id,
            {"setting": "in_flight_cap", "from": old, "to": body.in_flight_cap},
        )
    db.commit()
    return organisation_out(db, org, user)


@router.put(
    "/organisation/retention",
    response_model=OrganisationOut,
    dependencies=[allow(Action.run_organisation)],
)
def set_retention(body: RetentionIn, user: CurrentUser, org: CurrentOrganisation, db: DB):
    """Set how long runs and chat history are kept (null: indefinitely), audited when it
    changes; the next nightly prune (`aios.retention`) follows it."""
    old = retention_out(org)
    org.run_retention_days, org.chat_retention_days = body.run_days, body.chat_days
    new = retention_out(org)
    if new != old:
        audit.enqueue(
            db,
            org.id,
            audit.person(user.id),
            "settings.changed",
            "organisation",
            org.id,
            {"setting": "retention", "from": old.model_dump(), "to": new.model_dump()},
        )
    db.commit()
    return organisation_out(db, org, user)


@router.post(
    "/organisation/deletion",
    response_model=OrganisationOut,
    dependencies=[allow(Action.run_organisation)],
)
def schedule_deletion(body: DeletionIn, user: CurrentUser, org: CurrentOrganisation, db: DB):
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


@router.delete(
    "/organisation/deletion",
    response_model=OrganisationOut,
    dependencies=[allow(Action.run_organisation)],
)
def cancel_deletion(user: CurrentUser, org: CurrentOrganisation, db: DB):
    try:
        organisations.cancel_deletion(db, org, user)
    except organisations.DeletionRefused as refused:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(refused)) from None
    db.commit()
    return organisation_out(db, org, user)


# Invitation links (`aios.organisations`).


class InviteIn(BaseModel):
    expires_in_days: int = Field(organisations.INVITE_DAYS, ge=1, le=organisations.INVITE_DAYS_MAX)
    # The role whoever joins through it is given.
    role: Role = Role.editor


class InviteOut(BaseModel):
    id: uuid.UUID
    # The role whoever joins through it is given.
    role: Role
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
    # The role they'd join with.
    role: Role
    expires_at: datetime


def invite_out(invite: OrganisationInvite, db: Session) -> InviteOut:
    creator = db.get(User, invite.created_by) if invite.created_by is not None else None
    return InviteOut(
        id=invite.id,
        role=invite.role,
        creator=UserOut.model_validate(creator, from_attributes=True) if creator else None,
        created_at=invite.created_at,
        expires_at=invite.expires_at,
    )


@router.post(
    "/organisation/invites",
    response_model=NewInviteOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[allow(Action.run_organisation)],
)
def make_invite(body: InviteIn, user: CurrentUser, org: CurrentOrganisation, db: DB):
    invite, token = organisations.make_invite(db, org, user, body.expires_in_days, body.role)
    db.commit()
    return NewInviteOut(
        **invite_out(invite, db).model_dump(),
        token=token,
        url=f"{get_settings().public_url.rstrip('/')}/invite/{token}",
    )


@router.get(
    "/organisation/invites",
    response_model=list[InviteOut],
    dependencies=[allow(Action.run_organisation)],
)
def list_invites(org: CurrentOrganisation, db: DB):
    return [invite_out(i, db) for i in organisations.open_invites(db, org)]


@router.delete(
    "/organisation/invites/{invite_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[allow(Action.run_organisation)],
)
def revoke_invite(invite_id: uuid.UUID, user: CurrentUser, org: CurrentOrganisation, db: DB):
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
    audit.enqueue(db, org.id, audit.person(user.id), "invitation.revoked", "invitation", invite.id)
    db.commit()


@router.get("/invites/{token}", response_model=InvitationOut)
def open_invite(token: str, db: DB):
    invite = organisations.open_invite(db, token)
    if invite is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NO_SUCH_INVITE)
    org = db.get_one(Organisation, invite.organisation_id)
    return InvitationOut(
        organisation=InvitedToOut(id=org.id, name=org.name),
        role=invite.role,
        expires_at=invite.expires_at,
    )


@router.post("/invites/{token}/accept", response_model=OrganisationOut)
def accept_invite(token: str, user: CurrentUser, db: DB):
    invite = organisations.open_invite(db, token, lock=True)
    if invite is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NO_SUCH_INVITE)
    org = organisations.join(db, user, invite)
    db.commit()
    return organisation_out(db, org, user)


@router.post("/organisation/leave", response_model=MeOut, dependencies=[allow(Action.leave)])
def leave_organisation(user: CurrentUser, org: CurrentOrganisation, db: DB):
    if (refusal := organisations.leave_refusal(db, org, user)) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, refusal)
    organisations.leave(db, org, user)
    db.commit()
    return me_out(db, user)


class RoleIn(BaseModel):
    role: Role


NOT_A_MEMBER = "They aren't a member of this organisation."


@router.put(
    "/organisation/members/{user_id}/role",
    response_model=OrganisationOut,
    dependencies=[allow(Action.run_organisation)],
)
def change_role(user_id: int, body: RoleIn, user: CurrentUser, org: CurrentOrganisation, db: DB):
    """Give a member another role (an admin's): 404 for someone who isn't a member, 409 for
    demoting the organisation's last admin. Answers with the Organisation page."""
    try:
        changed = organisations.change_role(db, org, user_id, body.role, by=user.id)
    except organisations.RoleChangeRefused as refused:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(refused)) from None
    if not changed:
        db.rollback()
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_A_MEMBER)
    db.commit()
    return organisation_out(db, org, user)


def users_out(store_id: uuid.UUID, user_ids: set[int]) -> dict[int, UserOut]:
    """The store's organisation's people with these ids, by id, on a session of its own: for
    what records who did it in a store (its documents)."""
    if not user_ids:
        return {}
    with SessionLocal() as db:
        organisation_id = db.scalar(
            select(DataStore.owner_organisation_id).where(DataStore.id == store_id)
        )
        if organisation_id is None:
            return {}
        return people(db, organisation_id, user_ids)

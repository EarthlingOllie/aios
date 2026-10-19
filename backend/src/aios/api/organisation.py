"""The signed-in user (`GET /api/me`) and their organisation (`/api/organisation`, see
`aios.organisations`).

`/api/me` says who the user is and which organisation they're in, which the shell shows.
`GET /api/organisation` is the Organisation page: the name, the creator and the members with
when each joined; `PUT` renames it, for the creator only (a 403 for another member), to a name
no other organisation has (a 409 otherwise).
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from aios.api.skills import on_conflict
from aios.auth import CurrentOrganisation, CurrentUser
from aios.db import DB
from aios.models import Organisation, OrganisationMember, User

router = APIRouter(prefix="/api")

NAME_CONSTRAINT = "organisations_name_key"
NAME_TAKEN = "An organisation with that name already exists."
NOT_THE_CREATOR = "Only the organisation's creator can rename it."


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

"""Who did what to the things an organisation owns, as the API reports it.

Everything in an organisation is every member's, so each agent, skill, workflow and schedule
says which member made it (`created_by`) and which last changed it (`updated_by`), and each
version which member saved it; a soft-deleted one keeps who deleted it (`deleted_by`, not
reported: a deleted row isn't served). Each is a user id on the row, null once that user is
deleted, and reported as the user (`UserOut`), read for a whole list in one query
(`people`). Each run says which member started it the same way (`started_by`).
"""

from collections.abc import Iterable, Sequence

from pydantic import AliasChoices, BaseModel, Field
from sqlalchemy import Row, select
from sqlalchemy.orm import Session

from aios.models import User


class UserOut(BaseModel):
    id: int
    # Their full name ("First Last", `User.full_name`), read from a user as that.
    name: str = Field(validation_alias=AliasChoices("full_name", "name"))


class Provenance(BaseModel):
    """Who made it and who last changed it; null once that user is deleted (or, for rows from
    before organisations, where it isn't known)."""

    created_by: UserOut | None
    updated_by: UserOut | None


def made_by(org_id: int, user_id: int) -> dict:
    """The columns a new row of the organisation's (an agent, skill, workflow, trigger or chat)
    starts with, made by the member `user_id`: whose it is, and who made and last changed it."""
    return {"organisation_id": org_id, "created_by": user_id, "updated_by": user_id}


def people(db: Session, ids: Iterable[int | None]) -> dict[int, UserOut]:
    """The users with these ids (nulls skipped), by id, in one query."""
    wanted = {i for i in ids if i is not None}
    if not wanted:
        return {}
    return {
        u.id: UserOut.model_validate(u, from_attributes=True)
        for u in db.scalars(select(User).where(User.id.in_(wanted)))
    }


def started_by[M: BaseModel](db: Session, rows: Sequence[Row], model: type[M]) -> list[M]:
    """Rows of a run summary query as `model`s, with who started each (the row's
    `created_by_id`) as the user, read for them all in one query."""
    known = people(db, [row.created_by_id for row in rows])
    return [
        model.model_validate({**row._mapping, "created_by": known.get(row.created_by_id)})
        for row in rows
    ]


def provenance(db: Session, row, known: dict[int, UserOut] | None = None) -> dict:
    """`created_by` and `updated_by` of `row` (anything with those two columns), as users: from
    `known` (see `people`) when given, else read now."""
    if known is None:
        known = people(db, [row.created_by, row.updated_by])
    return {"created_by": known.get(row.created_by), "updated_by": known.get(row.updated_by)}

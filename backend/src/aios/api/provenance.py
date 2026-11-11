"""Who did what to the things an organisation owns, as the API reports it.

Everything in an organisation is every member's, so each agent, skill, workflow and schedule
says which member made it (`created_by`) and which last changed it (`updated_by`), and each
version which member saved it; an archived agent says which member archived it
(`archived_by`). Each is a user id on the row, null once that user is deleted, and reported as
the person (`UserOut`), resolved for a whole list at once through the organisation's store's
people mirror (`people`, `aios.datastores.people.resolve`): someone who has left the
organisation shows as a former member. Each run says which member started it the same way
(`started_by`).
"""

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any

from pydantic import AliasChoices, BaseModel, Field
from sqlalchemy import Row
from sqlalchemy.orm import Session

from aios.datastores import people as mirror


class UserOut(BaseModel):
    id: int
    # Their full name ("First Last", `User.full_name`), read from a user as that.
    name: str = Field(validation_alias=AliasChoices("full_name", "name"))
    # They've left the organisation (what they made stays, naming them).
    former_member: bool = False


class Provenance(BaseModel):
    """Who made it and who last changed it; null once that user is deleted (or, for rows from
    before organisations, where it isn't known)."""

    created_by: UserOut | None
    updated_by: UserOut | None


class Archived(BaseModel):
    """When it was archived and by which member: both null while it isn't (the member also null
    once that user is deleted)."""

    archived_at: datetime | None = None
    archived_by: UserOut | None = None


def archived(row, known: dict[int, UserOut]) -> dict:
    """`archived_at` and `archived_by` of an agent, skill or workflow, with the member from
    `known` (see `provenance.people`, which should have been given `row.archived_by`)."""
    return {"archived_at": row.archived_at, "archived_by": known.get(row.archived_by)}


def made_by(org_id: int, user_id: int) -> dict:
    """The columns a new row of the organisation's (an agent, skill, workflow, trigger or chat)
    starts with, made by the member `user_id`: whose it is, and who made and last changed it."""
    return {"organisation_id": org_id, "created_by": user_id, "updated_by": user_id}


def built_by(user_id: int) -> dict:
    """The columns a new agent, skill or workflow (in the organisation's store, whose rows are
    all its own) starts with, made by the member `user_id`: who made and last changed it."""
    return {"created_by": user_id, "updated_by": user_id}


def people(db: Session, organisation_id: int, ids: Iterable[int | None]) -> dict[int, UserOut]:
    """The organisation's people with these ids (nulls skipped), by id, from its store's people
    mirror (and from the platform for any it hasn't caught up with), all at once."""
    return {
        i: UserOut(id=i, name=p.name, former_member=p.former)
        for i, p in mirror.resolve(db, organisation_id, ids).items()
    }


def started_by[M: BaseModel](
    db: Session, organisation_id: int, rows: Sequence[Row | Mapping[str, Any]], model: type[M]
) -> list[M]:
    """Rows of a run summary query of the organisation's (or mappings of their columns) as
    `model`s, with who started each (the row's `created_by_id`) as the person, resolved for them
    all at once."""
    mappings = [row._mapping if isinstance(row, Row) else row for row in rows]
    known = people(db, organisation_id, [m["created_by_id"] for m in mappings])
    return [
        model.model_validate({**m, "created_by": known.get(m["created_by_id"])}) for m in mappings
    ]


def provenance(db: Session, row, known: dict[int, UserOut] | None = None) -> dict:
    """`created_by` and `updated_by` of `row` (anything of an organisation's with those two
    columns), as people: from `known` (see `people`) when given, else read now (for a row of the
    platform's, which says whose organisation it's in)."""
    if known is None:
        known = people(db, row.organisation_id, [row.created_by, row.updated_by])
    return {"created_by": known.get(row.created_by), "updated_by": known.get(row.updated_by)}

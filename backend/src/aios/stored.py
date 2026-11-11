"""What the platform's rows name in an organisation's store, read from it: the names of its
agents and workflows, and the numbers of their versions, by id.

Runs, schedules and chats (still on the platform) name an agent or workflow, and the version
a run pinned, by its id in the organisation's store. Ids are the store's own and repeat across
organisations, so each lookup is in one organisation's store. Each opens a read-only session on
it for the one query, whatever else the caller holds.
"""

from collections.abc import Iterable

from sqlalchemy import select

from aios.datastores import organisation_store
from aios.datastores.models import Agent, AgentVersion, Workflow, WorkflowVersion


def names(
    organisation_id: int, model: type[Agent] | type[Workflow], ids: Iterable[int | None]
) -> dict[int, str]:
    """The names of the organisation's agents or workflows with these ids (nulls skipped)."""
    wanted = {i for i in ids if i is not None}
    if not wanted:
        return {}
    with organisation_store(organisation_id, "ro") as store:
        return dict(store.execute(select(model.id, model.name).where(model.id.in_(wanted))).all())


def version_numbers(
    organisation_id: int,
    model: type[AgentVersion] | type[WorkflowVersion],
    ids: Iterable[int | None],
) -> dict[int, int]:
    """The numbers of the organisation's agent or workflow versions with these ids (nulls
    skipped), by id."""
    wanted = {i for i in ids if i is not None}
    if not wanted:
        return {}
    with organisation_store(organisation_id, "ro") as store:
        return dict(
            store.execute(select(model.id, model.version).where(model.id.in_(wanted))).all()
        )

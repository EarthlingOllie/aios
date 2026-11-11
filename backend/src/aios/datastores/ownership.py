"""Table ownership: which agent owns each data table, and which it's shared with
(`aios.table_owners`, `aios.table_shares`).

- **Owners.** Applying an approved schema change that creates a table records the agent behind
  the request's run as its owner, in the same transaction (`created`).
- **Who may write** (`write_refusal`, which `write_data` asks before it writes):
  - the owner, and the agents it's shared with;
  - for a table with no owner (made before owners, or cleared), any agent with the tool;
  - for a table whose owner is archived, nobody until it's transferred.

  Reads are unchanged: every agent reads every table through `query_data`.
- **Managing access.** Editors and admins assign an owner, transfer or clear it, and share and
  unshare (`set_owner`, `share`, `unshare`), each audited. Assigning an owner to a table that has
  none tightens access, so any editor may; transferring a table or clearing its owner is a
  change of ownership, which needs the role the `change_ownership` approval rule says
  (`aios.approval_rules`: an admin by default).
- **Drops, restores and purges.** The rows name a table as it's named now: dropping it renames
  them to its dropped name (`renamed`), restoring it renames them back, and purging it deletes
  them (`aios.datastores.dropped`). A share hangs off its table's ownership row, so it follows both.

The database doesn't enforce any of this yet: the write tool does. Changes take effect at once,
so the `pending` flag the API answers with is always false until per-agent roles carry them.
"""

from dataclasses import dataclass, field

import psycopg
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from aios import approval_rules, audit
from aios.datastores.dropped import VISIBLE_TABLE, is_dropped
from aios.datastores.models import Agent, TableOwner, TableShare
from aios.models import Role

SCHEMA = "data"


@dataclass(frozen=True)
class AgentRef:
    id: int
    name: str
    archived: bool


@dataclass
class Access:
    """Who may write a table: its owner (None: anyone with the tool), and the agents it's shared
    with, by name. `pending` until the database has applied a change (never, yet)."""

    owner: AgentRef | None = None
    shares: list[AgentRef] = field(default_factory=list)
    pending: bool = False


class TableNotFound(LookupError):
    """No data table of this name (that isn't dropped)."""


class AgentNotFound(LookupError):
    """No agent of this id in the store."""


class ShareNotFound(LookupError):
    """The table isn't shared with the agent."""


class Conflict(ValueError):
    """A change that can't be made as things are: with an archived agent, a share it already
    has, a share with its owner, or a share of a table with no owner."""


class NeedsRole(PermissionError):
    """The approval rules ask more of the member than their role."""


# --- Reading ------------------------------------------------------------------------------------


def _ref(agent: Agent) -> AgentRef:
    return AgentRef(agent.id, agent.name, agent.archived_at is not None)


def access(store: Session, tables: list[str]) -> dict[str, Access]:
    """Each named table's access (a table with no row: no owner, no shares)."""
    out = {name: Access() for name in tables}
    if not tables:
        return out
    owners = store.execute(
        select(TableOwner.table_name, Agent)
        .join(Agent, Agent.id == TableOwner.agent_id)
        .where(TableOwner.table_name.in_(tables))
    ).all()
    for table, agent in owners:
        out[table].owner = _ref(agent)
    shares = store.execute(
        select(TableShare.table_name, Agent)
        .join(Agent, Agent.id == TableShare.agent_id)
        .where(TableShare.table_name.in_(tables))
        .order_by(TableShare.table_name, Agent.name, Agent.id)
    ).all()
    for table, agent in shares:
        out[table].shares.append(_ref(agent))
    return out


def owned_by(store: Session, agent_id: int) -> list[str]:
    """The data tables the agent owns (not dropped ones), by name."""
    names = store.scalars(
        select(TableOwner.table_name)
        .where(TableOwner.agent_id == agent_id)
        .order_by(TableOwner.table_name)
    ).all()
    return [name for name in names if not is_dropped(name)]


# `write_data`'s check, as the read-write role, in the write's transaction: the table's owner
# (name and whether it's archived, if it has one), and whether the run's agent owns it or has
# a share. Share-locks the ownership row, so a transfer waits for writes in flight.
_WRITE_SQL = """
    WITH me AS (SELECT agent_id FROM aios.agent_runs WHERE id = %(run_id)s)
    SELECT o.agent_id, a.name, a.archived_at IS NOT NULL,
           o.agent_id = (SELECT agent_id FROM me)
           OR EXISTS (SELECT FROM aios.table_shares s
                      WHERE s.table_name = o.table_name AND s.agent_id = (SELECT agent_id FROM me))
    FROM aios.table_owners o
    LEFT JOIN aios.agents a ON a.id = o.agent_id
    WHERE o.table_name = %(table)s
    FOR SHARE OF o
"""


def write_refusal(pg: psycopg.Connection, table: str, run_id: int | None) -> str | None:
    """Why the run's agent may not write `data.<table>`, naming its owner; None if it may (the
    table has no owner, or the agent owns it or has a share)."""
    row = pg.execute(_WRITE_SQL, {"table": table, "run_id": run_id}).fetchone()
    if row is None or row[0] is None:
        return None
    _, owner, archived, mine = row
    qualified = f"`{SCHEMA}.{table}`"
    if archived:
        return (
            f"{qualified} is read-only: its owner, the agent {owner}, is archived. Nothing was"
            " written. You can still read it with `query_data`; an editor can transfer it to"
            " another agent."
        )
    if mine:
        return None
    return (
        f"{qualified} is owned by the agent {owner}: only it and the agents it's shared with may"
        " write it. Nothing was written. You can still read it with `query_data`; an editor can"
        " share it with you."
    )


# --- Schema changes -----------------------------------------------------------------------------


def created(conn: psycopg.Connection, table: str, run_id: int | None) -> None:
    """Record the agent behind `run_id` as the owner of `data.<table>`, which a schema change it
    asked for has just made (as the store's owner, in the change's transaction). A stale row of
    that name (a table dropped by hand, say) goes first, with its shares."""
    conn.execute("DELETE FROM aios.table_owners WHERE table_name = %s", (table,))
    if run_id is None:
        return
    conn.execute(
        "INSERT INTO aios.table_owners (table_name, agent_id)"
        " SELECT %s, agent_id FROM aios.agent_runs WHERE id = %s",
        (table, run_id),
    )


def renamed(conn: psycopg.Connection, old: str, new: str) -> None:
    """Carry a table's ownership (and its shares, by their foreign key) to its new name, as a
    drop renames it (as the store's owner, in the change's transaction). Restores and purges do
    the same in their own statements (`aios.datastores.dropped`)."""
    conn.execute("UPDATE aios.table_owners SET table_name = %s WHERE table_name = %s", (new, old))


# --- Managing access ----------------------------------------------------------------------------

_EXISTS_SQL = f"""
    SELECT FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = %s AND c.relname = %s AND c.relkind IN ('r', 'p')
      AND NOT c.relispartition AND {VISIBLE_TABLE}
"""


def _check_table(store: Session, table: str) -> None:
    pg = store.connection().connection.driver_connection
    if is_dropped(table) or pg.execute(_EXISTS_SQL, (SCHEMA, table)).fetchone() is None:
        raise TableNotFound(table)


def _row(store: Session, table: str) -> TableOwner:
    """The table's ownership row, made (with no owner) if it has none, and locked until
    commit."""
    _check_table(store, table)
    store.execute(
        insert(TableOwner)
        .values(table_name=table)
        .on_conflict_do_nothing(index_elements=[TableOwner.table_name])
    )
    return store.scalars(
        select(TableOwner)
        .where(TableOwner.table_name == table)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()


def _live_agent(store: Session, agent_id: int, what: str) -> Agent:
    """The agent, share-locked until commit (so archiving it waits for the change); a
    `Conflict` if it's archived."""
    agent = store.scalars(
        select(Agent)
        .where(Agent.id == agent_id)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    ).one_or_none()
    if agent is None:
        raise AgentNotFound(agent_id)
    if agent.archived_at is not None:
        raise Conflict(f"The agent {agent.name} is archived, so it can't {what}.")
    return agent


def _audit(store: Session, user_id: int, action: str, table: str, **details) -> None:
    audit.record(
        store, audit.person(user_id), f"table.{action}", "table", table, {"table": table} | details
    )


def _named(agent: Agent | None) -> dict | None:
    return None if agent is None else {"id": agent.id, "name": agent.name}


def set_owner(store: Session, table: str, agent_id: int | None, user_id: int, role: Role) -> Access:
    """Make the agent (None: nobody) the table's owner, as the member `user_id` with `role`, in
    the caller's store transaction; audited. Assigning an owner to a table with none needs only
    an editor; transferring or clearing it needs the `change_ownership` rule's role
    (`NeedsRole`). A new owner that had a share loses it (it owns the table now). Raises
    `TableNotFound`, `AgentNotFound`, `Conflict` (an archived agent)."""
    row = _row(store, table)
    before = store.get(Agent, row.agent_id) if row.agent_id is not None else None
    agent = None
    if agent_id is not None:
        agent = _live_agent(store, agent_id, "own a table")
    if row.agent_id == agent_id:
        return access(store, [table])[table]
    if before is not None:
        least = approval_rules.current(store)["change_ownership"]
        if not approval_rules.at_least(role, least):
            raise NeedsRole(
                f"Changing a table's owner needs {approval_rules.a(least)}, by the organisation's"
                " approval rules."
            )
    row.agent_id = agent_id
    row.set_by = user_id
    row.set_at = func.now()
    store.execute(
        delete(TableShare).where(TableShare.table_name == table, TableShare.agent_id == agent_id)
    )
    action = "owner_assigned" if before is None else "transferred" if agent else "owner_cleared"
    _audit(store, user_id, action, table, **{"from": _named(before), "to": _named(agent)})
    store.flush()
    return access(store, [table])[table]


def share(store: Session, table: str, agent_id: int, user_id: int) -> Access:
    """Share the table with the agent, as the member `user_id`, in the caller's store
    transaction; audited. Raises `TableNotFound`, `AgentNotFound`, `Conflict` (an archived
    agent, the owner, a table with no owner, or one it's shared with already)."""
    row = _row(store, table)
    agent = _live_agent(store, agent_id, "be given a table")
    if row.agent_id is None:
        raise Conflict(
            f"`{SCHEMA}.{table}` has no owner, so any agent may write it: assign one before"
            " sharing it."
        )
    if row.agent_id == agent.id:
        raise Conflict(f"The agent {agent.name} owns `{SCHEMA}.{table}` already.")
    made = store.execute(
        insert(TableShare)
        .values(table_name=table, agent_id=agent.id, granted_by=user_id)
        .on_conflict_do_nothing()
        .returning(TableShare.agent_id)
    ).first()
    if made is None:
        raise Conflict(f"`{SCHEMA}.{table}` is shared with the agent {agent.name} already.")
    _audit(store, user_id, "shared", table, agent=_named(agent))
    return access(store, [table])[table]


def unshare(store: Session, table: str, agent_id: int, user_id: int) -> Access:
    """Stop sharing the table with the agent, as the member `user_id`, in the caller's store
    transaction; audited. Raises `TableNotFound`, `ShareNotFound`."""
    _row(store, table)
    gone = store.execute(
        delete(TableShare)
        .where(TableShare.table_name == table, TableShare.agent_id == agent_id)
        .returning(TableShare.agent_id)
    ).first()
    if gone is None:
        raise ShareNotFound(agent_id)
    _audit(store, user_id, "unshared", table, agent=_named(store.get(Agent, agent_id)))
    return access(store, [table])[table]

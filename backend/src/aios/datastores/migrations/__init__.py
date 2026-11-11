"""The data store Alembic tree: the platform-managed schemas in every store, apart from the
platform database's own tree (`backend/alembic`).

It has two lines, each with its own head:

- `aios` (revisions `0001`, `0002`, ...), which every store is at the head of.
- `history` (the branch labelled `history`, revisions `h0001`, ...): the run history export's
  tables, which a store gets only once its organisation switches the export on, and is then kept at
  the head of too (`aios.datastores.history`).

Its version table is `aios.alembic_version`, in each store: a row per line the store has. `data`,
the organisation's own tables, is never touched by it. `aios.datastores.provision` runs it, as the
store's owner role; there's no alembic.ini for it. To add a revision, copy the latest file of its
line in versions/ and set its `down_revision` to that file's `revision`.

A table it makes is readable by `_ro` and writable by `_rw`, but for its own bookkeeping
(`provision.BOOKKEEPING`), which only it writes: add any new such table there.

Stores are upgraded one at a time while the new release is already running, so every revision
must work with the code before it (expand now, contract in a later release).
"""

from functools import cache
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Connection

from aios.datastores.models import SCHEMA, StoreBase

HERE = Path(__file__).parent
# Where the tree's revisions are. Tests add a directory of their own.
VERSION_LOCATIONS = [str(HERE / "versions")]


def config(connection: Connection | None = None) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(HERE))
    cfg.set_main_option("version_locations", " ".join(VERSION_LOCATIONS))
    cfg.set_main_option("path_separator", "space")
    cfg.attributes["connection"] = connection
    return cfg


def include_name(name, type_, parent_names) -> bool:
    # The organisation's tables (`data`) are changed only by approved schema change requests,
    # and the checkpointer's (`langgraph`) by LangGraph's own migrations (`aios.checkpoints`).
    return not (type_ == "schema" and name in ("data", "langgraph"))


def context_options() -> dict:
    """How Alembic sees a store: its tables are `aios.datastores.models`', in `aios`."""
    return {
        "target_metadata": StoreBase.metadata,
        "version_table_schema": SCHEMA,
        "include_schemas": True,
        "include_name": include_name,
    }


# The branch label of the run history export's line.
HISTORY = "history"


def _line(history: bool) -> list[str]:
    """The revisions of the `aios` line, or of the `history` line, head first."""
    return list(_lines(tuple(VERSION_LOCATIONS))[history])


# Opening a store checks it isn't behind, so the tree is read once, not on every request.
@cache
def _lines(locations: tuple[str, ...]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    script = ScriptDirectory.from_config(config())
    revisions = list(script.walk_revisions())
    return (
        tuple(s.revision for s in revisions if HISTORY not in s.branch_labels),
        tuple(s.revision for s in revisions if HISTORY in s.branch_labels),
    )


def head_revision() -> str:
    """The `aios` line's head: the revision every ready store is brought to."""
    return _line(history=False)[0]


def revisions_before_head() -> list[str]:
    """The `aios` line's revisions before its head: a store at one of them is behind."""
    return _line(history=False)[1:]


def history_head() -> str:
    """The `history` line's head: the revision a store with run history is brought to."""
    return _line(history=True)[0]


def history_revisions_before_head() -> list[str]:
    """The `history` line's revisions before its head."""
    return _line(history=True)[1:]


def is_history_revision(revision: str) -> bool:
    return revision in _line(history=True)

"""The data store Alembic tree: the platform-managed schemas in every store (`aios`, later
`history`), apart from the platform database's own tree (`backend/alembic`).

Its version table is `aios.alembic_version`, in each store. `data`, the owner's own tables,
is never touched by it. `aios.datastores.provision` runs it, as the store's owner role; there's
no alembic.ini for it. To add a revision, copy the latest file in versions/ and set its
`down_revision` to that file's `revision`.

Stores are upgraded one at a time while the new release is already running, so every revision
must work with the code before it (expand now, contract in a later release).
"""

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
    # The owner's tables (`data`) are changed only by approved schema change requests.
    return not (type_ == "schema" and name == "data")


def context_options() -> dict:
    """How Alembic sees a store: its tables are `aios.datastores.models`', in `aios`."""
    return {
        "target_metadata": StoreBase.metadata,
        "version_table_schema": SCHEMA,
        "include_schemas": True,
        "include_name": include_name,
    }


def head_revision() -> str:
    """The tree's head: the revision every ready store is brought to."""
    return ScriptDirectory.from_config(config()).get_current_head()


def revisions_before_head() -> list[str]:
    """The tree's revisions before its head: a store at one of them is behind."""
    script = ScriptDirectory.from_config(config())
    head = script.get_current_head()
    return [s.revision for s in script.walk_revisions() if s.revision != head]

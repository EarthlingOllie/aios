"""Schema change requests: how an agent changes the tables in its store's `data` schema.

The model never sends SQL. It sends a spec (`SchemaChange`): a list of operations
(`create_table`, `add_column`, `create_index`, `drop_table`, `drop_column`), each naming
tables, columns and types from an allowlist. The spec is checked against the store's current
schema (`check`) and compiled to SQL (`compile_sql`) with every identifier quoted; that SQL is
what a member approves, and exactly what runs. Only the `data` schema is touched, apart from
the platform's own bookkeeping: `aios.embedding_columns`, which marks embedding columns for
`write_data`, and `aios.dropped_objects`.

Drops are reversible: they rename the table or column to `_dropped_<timestamp>_<name>` and
record it in `aios.dropped_objects`, and applying one takes the store's login roles' access
to it away (`aios.datastores.dropped`), so the data tools no longer see it. Any member can
restore it until it's purged, `DATA_STORE_DROPPED_RETENTION_DAYS` later. Approving a request
that drops anything takes the names of the tables it drops from, typed (`confirmation`).

The flow (see `aios.tools.schema_change` and `aios.api.schema_changes`):

1. `propose` checks a spec against the store's schema (read as its read-only role), compiles
   it, and records a `SchemaChangeRequest` (in the store, `aios.schema_change_requests`) at the
   store's current data schema revision (`read_revision`: the latest `aios.schema_history`
   entry), and then notifies each user who can decide it that an approval is needed
   (`aios.notifications`, on the platform, naming the request by its id).
2. A member approves or rejects it (only users who pass `resolve.can_approve`), which clears
   its notifications once the store has the decision (`clear_notifications`).
3. `apply`, on the urgent worker (only the maintenance workers have the provisioner's
   credentials), runs an approved request's SQL as the store's owner, in one transaction
   under a `lock_timeout` (`SCHEMA_CHANGE_LOCK_TIMEOUT`), and appends it to
   `aios.schema_history`, which bumps the revision. If the revision has moved on since the
   request was made, the spec is checked again first, and a request that no longer applies is
   marked `stale` instead.
4. The run that asked is resumed with the `outcome`.

Split into `spec`, `compile`, `apply` and `expiry`. Every name is re-exported here, where callers
import it; `read_schema` is read here at call time, so a test that replaces it here replaces it
for every part.
"""

from aios.datastores.models import EMBEDDING_DIM
from aios.datastores.schema_changes.apply import (
    apply,
    find,
    lock_run,
    outcome,
    propose,
    read_revision,
    read_schema,
    record_history,
    schema_transaction,
    table_exists,
)
from aios.datastores.schema_changes.compile import (
    compile_sql,
    confirmation,
)
from aios.datastores.schema_changes.expiry import (
    StoredRequest,
    cancel,
    clear_notifications,
    clear_notifications_of,
    expire,
    expired_not_resumed,
    not_given,
    restore_notifications,
)
from aios.datastores.schema_changes.spec import (
    DROPPED_PATTERN,
    DROPPED_PREFIX,
    HNSW_MAX_DIMENSIONS,
    NAME_PATTERN,
    RESERVED,
    SCHEMA,
    AddColumn,
    Column,
    ColumnType,
    CreateIndex,
    CreateTable,
    Description,
    DropColumn,
    DroppedName,
    DropTable,
    Found,
    Invalid,
    Name,
    Op,
    Operation,
    SchemaChange,
    StoreSchema,
    check,
    summarize,
)

__all__ = [
    "AddColumn",
    "Column",
    "ColumnType",
    "CreateIndex",
    "CreateTable",
    "DROPPED_PATTERN",
    "DROPPED_PREFIX",
    "Description",
    "DropColumn",
    "DropTable",
    "DroppedName",
    "EMBEDDING_DIM",
    "Found",
    "HNSW_MAX_DIMENSIONS",
    "Invalid",
    "NAME_PATTERN",
    "Name",
    "Op",
    "Operation",
    "RESERVED",
    "SCHEMA",
    "SchemaChange",
    "StoreSchema",
    "StoredRequest",
    "apply",
    "cancel",
    "check",
    "clear_notifications",
    "clear_notifications_of",
    "compile_sql",
    "confirmation",
    "expire",
    "expired_not_resumed",
    "find",
    "lock_run",
    "not_given",
    "outcome",
    "propose",
    "read_revision",
    "read_schema",
    "record_history",
    "restore_notifications",
    "schema_transaction",
    "summarize",
    "table_exists",
]

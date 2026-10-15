# User databases: a Postgres database per user, for the data their agents gather

Status: **plan** (2026-10-15).

The platform is in development, with no customer data. Nothing has to be migrated: the
`documents` and `chunks` tables leave the platform database, and dev data is discarded.

Teams come after this work. Wherever a decision would be hard to undo, this plan keys it to the
data store rather than the user, so that a team can own one later (see
[Ready for teams](#ready-for-teams)).

## Goal

Every user gets a Postgres database of their own, their **data store**. We provision it, and only
the platform changes its schema. It holds what the user's agents and workflows gather for them:

- their uploaded documents and chunks (text, embeddings, full-text index)
- the tables agents create for them
- optionally, a copy of their run history

Raw uploaded files go to S3, under the data store's prefix. Postgres keeps only their metadata.

Everything the platform needs to run (users, agents, skills, workflows, runs, chat history)
stays in the platform database.

- **Platform database**: the platform's own data. Every platform feature that needs data stores
  it here.
- **Data store**: the user's data. Agents and workflows read and write it only through platform
  tools. Its schema grows only through **schema change requests**, which agents propose and the
  owner approves. Workflow runs pause until the owner decides.
- **Run history** can be copied into the data store if the user switches it on. That's a one-way
  export. The platform database stays the source of truth.

Users never connect to their data store directly. Only the platform does, from inside the VPC.

All data stores share the existing RDS instance, one database each. Each has its own generated
roles, so no store's credentials reach another store or the platform database. The platform
records where each store lives, so moving some to another instance later is a data move, not a
redesign.

## Architecture

```
                      ┌──────────────── RDS instance (Postgres 17 + pgvector) ─────────────────┐
api ──────────────────┼─▶ aios (platform DB)                                                    │
workflow-worker       │     users, agents, skills, workflows, *_versions, agent_runs,           │
worker                │     workflow_runs, langgraph.* (checkpoints), sandbox_sessions,         │
  │                   │     data_stores, schema_change_requests, history_exports                │
  │                   │                                                                         │
  ├── as ds_<id>_rw ──┼─▶ ds_<id> (one per data store)                                          │
  │   (per store)     │     aios.*    platform-managed: documents, chunks, schema_history,      │
  │                   │               alembic_version                                           │
provisioner ──────────┼─▶   data.*    agent-defined tables, changed only by approved requests   │
(CREATEDB, CREATEROLE)│     history.* optional copy of the owner's run history                  │
  │                   └─────────────────────────────────────────────────────────────────────────┘
  └──────────────────────▶ S3: aios-data-<env>/stores/<store id>/documents/<document id>
```

### What lives where

| Data | Where | Notes |
| --- | --- | --- |
| `users`, sessions, settings | Platform DB | |
| Agents, skills, workflows, their versions and nodes | Platform DB | Platform features |
| `agent_runs`, LangGraph checkpoints, `workflow_runs`, `workflow_run_steps` | Platform DB | Source of truth for run history |
| `sandbox_sessions` | Platform DB | Runtime state and secrets |
| `data_stores`, `schema_change_requests`, `history_exports` | Platform DB | New: the machinery that manages data stores |
| Document metadata and chunks (text, embeddings, FTS) | Data store, `aios` schema | Moved off the platform DB |
| Raw uploaded files | S3, `stores/<store id>/documents/<document id>` | Replaces `documents.data` |
| Tables agents create | Data store, `data` schema | Created and changed only by approved schema change requests |
| Run history copy | Data store, `history` schema | Exported while the owner has it switched on. Kept if they switch it off. |

### Three schemas in each data store

- **`aios`**: tables every data store has (documents, chunks, the schema change log). The
  platform owns them and migrates them with their own Alembic tree. Agents read and write them
  only through platform tools, and never change their structure.
- **`data`**: the owner's tables. They're created and changed only through schema change
  requests. Alembic ignores this schema.
- **`history`**: the run history export, created when the owner first switches it on. The
  platform owns its structure, as part of the data store's Alembic tree.

## Ready for teams

Today every data store belongs to one user. Later, teams will own data stores too: a team's
members share its store, and its agents and workflows write to it. The parts that are hard to
change later are fixed now so that this needs no data move:

- **Data stores have their own id.** `data_stores.id` is a UUID, not the user's id. Every name
  derived from it uses that id: the database (`ds_<hex>`), the roles (`ds_<hex>_rw`, ...) and the
  S3 prefix (`stores/<uuid>/`). Nothing in Postgres or S3 is named after a user, so handing a
  store to a team renames nothing.
- **Ownership is one column that can be widened.** `data_stores.owner_user_id` is unique for now
  (one store per user). Teams add `owner_team_id` and a check that exactly one of the two is set.
- **Code finds a store through one function.** `data_store_for(user)` is the only place a user
  becomes a store. Everything below it takes a `data_store_id`:
  - `ChatDeps` (the run context) carries `data_store_id`, resolved when the run starts.
  - Celery tasks take `data_store_id`, not `user_id`.
  - The connection resolver, the tools and the S3 helpers take `data_store_id` too.

  Teams then change only how a run picks its store (the workspace it runs in), not the tools or
  tasks.
- **Rows that refer to people say what they did.** Fields like
  `schema_change_requests.requested_by_run` and `decided_by_user_id` record who did what. They
  don't stand in for who owns the store.
- **Approval is a permission check, not an identity check.** `can_approve(user, store)` is "is the
  owner" today. Teams give it a role model later without touching the approval flow.
- **Exported history is keyed by the platform's ids, and records the user who ran each run.** A
  team store's history can then show which member ran what.

Teams will also have to change how agents, skills and workflows are owned (`owner_id` on each).
That's outside this plan, but nothing here makes it harder.

## Provisioning and isolation

### Roles

The platform database's connections stop using the RDS master user:

- `aios_app`: the API and workers' role for the platform database. No `CREATEDB` or `CREATEROLE`.
- `aios_provisioner`: `CREATEDB` and `CREATEROLE`. Only the provisioning tasks use it, to create
  and drop data stores and roles and to apply schema changes. Its credentials are a separate
  secret, given only to the worker that runs those tasks.

Each data store `ds_<id>` has three roles:

| Role | Login | Privileges |
| --- | --- | --- |
| `ds_<id>_owner` | No | Owns every schema and table in the database. The provisioner `SET ROLE`s to it to run migrations and approved changes. |
| `ds_<id>_rw` | Yes | `CONNECT`; `USAGE` on the schemas; `SELECT`, `INSERT`, `UPDATE`, `DELETE` on tables (via `ALTER DEFAULT PRIVILEGES FOR ROLE ds_<id>_owner`). No `CREATE` anywhere. |
| `ds_<id>_ro` | Yes | `CONNECT`; `USAGE`; `SELECT` only. Runs the queries agents write. |

All three roles are internal to the platform. Users never get credentials.

The login roles carry their own limits: `statement_timeout`,
`idle_in_transaction_session_timeout` and `CONNECTION LIMIT`, set per role from configuration.
One store can't hold the instance's connections or run unbounded queries.

Because the runtime roles can't run DDL, "only approved changes alter the schema" is enforced by
Postgres, not just by our code.

### Isolation on a shared instance

RDS doesn't let us edit `pg_hba.conf`, so isolation comes from privileges:

- `REVOKE CONNECT ... FROM PUBLIC` on every database: the platform's, `postgres`, `template1` and
  each store's. Then grant `CONNECT` only to that database's own roles.
- Drop the `public` schema in data stores.
- The catalogs `pg_database` and `pg_roles` are visible to every role, which is another reason
  names are opaque ids.
- `pg_stat_activity` already hides other roles' query text from non-superusers.
- Data stores get only `vector`: no `dblink`, `postgres_fdw` or untrusted languages.
- `template1` stays empty, since new databases copy it.
- RDS stays in private subnets with no public endpoint. No user ever connects directly.

A test asserts each of these: one store's roles can't connect to another store or to the platform
database, and can't create anything.

### The `data_stores` table (platform DB)

| Column | Notes |
| --- | --- |
| `id` | UUID. Names the database, roles and S3 prefix. |
| `owner_user_id` | Unique for now. Teams add `owner_team_id`. |
| `host`, `port`, `dbname` | Where it lives |
| `role_prefix` | The role names are derived from it |
| `rw_password`, `ro_password` | Encrypted with a key from Secrets Manager (`DATA_STORE_CREDENTIALS_KEY`; Fernet or KMS envelope encryption). Generated with `secrets`; never logged, never returned by the API. |
| `status` | `provisioning`, `ready`, `failed`, `deleting`, `deleted` |
| `schema_revision` | The data store Alembic revision it's at |
| `created_at`, `delete_after`, `error` | |

Rotating a password means `ALTER ROLE ... PASSWORD`, updating the row, then evicting cached
engines.

### Lifecycle

- **Sign-up**: `auth_callback` calls `data_store_for(user)`. That creates the `data_stores` row as
  `provisioning` and enqueues `aios.provision_data_store` on its own queue.
  - The task runs `CREATE DATABASE`, creates the roles, runs `CREATE EXTENSION vector`, revokes and
    grants as above, runs the data store migrations, then marks the store `ready`.
  - Each step can safely be re-run, so a retry after a crash finishes the job.
- **Until it's ready**: uploads, search and data tools answer "your data store is being set up"
  (409). Runs whose agent has data tools refuse to start. The UI shows that state.
- **Deleting a user**: the store is marked `deleting`, with
  `delete_after = now() + DATA_STORE_DELETED_RETENTION_DAYS`. Its connections are refused straight
  away.
  - A scheduled task then terminates any connections, runs `DROP DATABASE ... WITH (FORCE)`, drops
    the roles and deletes the S3 prefix, and marks the store `deleted`.
  - Until then, an operator can restore it.
- **Moving instances later**: `pg_dump` / `pg_restore`, then update `host` and `port`.

## Reaching a data store from code

A new package, `aios/datastores/`:

- **`resolve.py`**: `data_store_for(user)`, the single seam between users and stores (see
  [Ready for teams](#ready-for-teams)).
- **`connections.py`**: `store_session(data_store_id, role="rw")` and an async equivalent. They
  load the store's row, decrypt the credentials and open a session.
  - Engines are cached per (store, role) in a small LRU, with small pools
    (`pool_size=1, max_overflow=2`), and evicted when idle.
  - PgBouncer or RDS Proxy can go in front later without changing callers.
- **`files.py`**: S3 put, get and delete under a store's prefix, and signed download URLs.
- **`provision.py`**: create, migrate and drop stores, run as `aios_provisioner`.
- **`schema_changes.py`**: the change spec, its compiler to SQL, and its application.
- **`quota.py`**: storage usage and checks.
- **`migrations/`**: the data store Alembic tree (`aios` and `history` schemas). Its version table
  is `aios.alembic_version`, and an `include_name` hook excludes `data`.

Changes to existing code:

- **`db.py`** keeps the platform engine (`SessionLocal`, `DB`). Nothing user-owned goes through
  it.
- **Documents**:
  - `Document` and `Chunk` move to the data store's metadata. `owner_id` and `data` are dropped.
  - `documents` gains `s3_key`, `size_bytes` and `sha256`.
  - A platform migration drops both tables from the platform database. There's no data to move.
- **Uploads**: `api/documents.py` streams the file to S3, then inserts the `documents` row in the
  store, then enqueues the ingest. It deletes the object again if the insert or the enqueue fails.
- **Ingest**: `ingest_document_task(data_store_id, document_id)` reads the bytes from S3.
- **Downloads**: `GET /api/documents/{id}/download` checks the user can read the store, then
  redirects (302) to a presigned S3 `GetObject` URL.
  - The URL is valid for `DOCUMENT_DOWNLOAD_URL_SECONDS` (default 60).
  - It sets `ResponseContentDisposition` to an attachment with the original filename, so the
    browser saves the file rather than rendering it on our origin.
  - A presigned URL is a bearer link: anyone holding it can download the file until it expires.
    That's why it lives only a minute, and is minted per click rather than shown on the page.
  - Locally, RustFS's public endpoint signs the URLs (`DATA_STORE_FILES_PUBLIC_ENDPOINT`), so the
    browser can reach them.
- **Search**: `search.py` and `tools/hybrid_search.py` use `store_session(data_store_id, "ro")`.
  The search SQL itself doesn't change, apart from losing the `owner_id` filter.
- **Run context**: `ChatDeps` gains `data_store_id`, set when a run or chat turn starts.
- **Migrations**:
  - `alembic/` stays the platform tree.
  - An `aios.migrate_data_stores` task, plus a `deploy.sh` step after the platform migration,
    brings every `ready` store to head and records its revision.
  - Data store migrations must be backwards compatible (expand, then contract in a later release),
    because stores are upgraded one at a time while the new code is already running.
  - A failure stops that store, not the rollout, and is reported.

## Agent tools for the data store

New tools in the tool library. Each has settings like any other, so agent versions pin them.

| Key | What it does | Runs as |
| --- | --- | --- |
| `describe_data` | Lists the tables in `data` (and the readable `aios` tables), with columns, types and descriptions (`COMMENT ON`), read from the catalog | `ro` |
| `query_data` | Runs one read-only SQL statement. Results are capped in rows and bytes. | `ro`, in a `READ ONLY` transaction |
| `write_data` | Inserts, upserts, updates or deletes rows in a `data` table, given as structured arguments that the platform turns into SQL. Columns of type `embedding` are embedded on the way in. | `rw` |
| `request_schema_change` | Proposes a schema change, then pauses the run until the owner decides | Nothing: it records the request and waits |

- `write_data` is the only way agents write. There's no raw DML tool.
  - Updates and deletes must name the rows they touch, by primary key or by equality filters on
    columns.
  - A call has a row cap, from configuration.
  - Writes past the storage quota are refused.
- What these tools return is data, not instructions. Stored rows can carry prompt injection like
  any tool result, and the docstrings say so.
- `run_python` gets no database credentials. If code in the sandbox needs data, the agent queries
  it and passes it in.

## Schema change requests

### What an agent can ask for

A request is a list of operations in a structured spec (a Pydantic model), never SQL:

- `create_table`: name, description, columns, primary key (a `bigint` identity `id` by default)
- `add_column`: table, name, type, nullable, default, description
- `create_index`: btree, GIN for `jsonb` and full-text search, or HNSW for a vector column
- `drop_column`, `drop_table`: destructive, see below

Column types come from an allowlist: `text`, `integer`, `bigint`, `numeric`, `boolean`, `date`,
`timestamptz`, `jsonb`, `uuid` and `vector(n)`. There's also an `embedding` type: a
`vector(EMBEDDING_DIM)` column tied to a text column, which `write_data` fills with the
platform's embedding model.

Names must match `^[a-z][a-z0-9_]{0,62}$` and mustn't be reserved words. Every identifier is
quoted by the compiler. Operations only ever touch the `data` schema.

### The flow

1. **Request.**
   - The tool validates the spec against the store's current schema: a new table must not already
     exist, a column must exist to be dropped, and so on.
   - It compiles the spec to SQL and stores a row in `schema_change_requests` (platform DB):
     - `id`, `data_store_id`, `ops` (jsonb), `sql` (the compiled preview), `reason` (the agent's
       explanation)
     - where it came from: `requested_by_run` (the agent run), plus the workflow run and step if
       any
     - `base_revision`: the store's schema revision when it was made
     - `status`: `pending`, `approved`, `rejected`, `expired`, `applied`, `failed`, `stale`,
       `cancelled` (the run that asked was deleted, or can no longer resume)
     - `decided_by_user_id`, `decided_at`, `applied_at`, `error`
   - Then the tool calls LangGraph's `interrupt()` with the request's id, which pauses the agent run
     at that tool call, with its state saved in its checkpoint.
2. **Review.**
   - A *Data* page lists pending requests with the agent's reason and the exact SQL. The owner is
     notified in the app (see [Notifications](#notifications)).
   - The owner approves or rejects each one. Only users who pass `can_approve(user, store)` can
     decide: today, the store's owner.
   - Destructive operations show what they would remove (row counts) and need the table's name
     typed to confirm.
   - Requests expire after `SCHEMA_CHANGE_REQUEST_EXPIRY_DAYS`, which counts as a rejection.
3. **Apply** (on approval). `aios.apply_schema_change` runs on the provisioner queue:
   - It connects as `aios_provisioner`, runs `SET ROLE ds_<id>_owner`, and runs the SQL in one
     transaction, so a request applies fully or not at all.
   - It sets a `lock_timeout`, so it can't stall the store's other queries for long.
   - It appends a row to `aios.schema_history` in the store and bumps the revision.
   - If the schema has changed since `base_revision`, it validates the request again. A request
     that no longer applies is marked `stale` instead of failing halfway.
4. **Resume.**
   - Once the request is `applied`, `rejected`, `expired`, `stale` or `failed`, the paused run is
     resumed with `Command(resume=<outcome>)`.
   - The tool call returns that outcome to the model: "applied, table `data.contacts` now exists",
     or "rejected: <the owner's note>". The agent carries on from there.

### Pausing runs

**Workflow runs** must pause until the owner decides. A decision can take days, so a paused run
holds no worker:

- **New statuses.** `waiting` is added to both `WorkflowRunStatus` and `WorkflowStepStatus`. A
  waiting step records the request it waits on (`waiting_on_request_id`).
- **What happens when a node's agent hits the interrupt:**
  - That step becomes `waiting`.
  - Other branches carry on, and nodes that don't depend on the waiting one still start.
  - Once nothing else can run, the run becomes `waiting` and its Celery task returns, freeing the
    worker.
- **What waiting runs are exempt from:**
  - The stale-heartbeat check (`alive`): a waiting run is not a dead run.
  - The run time limit (`WORKFLOW_RUN_TIME_LIMIT`), which counts running time only. The run
    records time spent waiting.
- **Resuming:**
  - The decision re-enqueues `aios.run_workflow` with the run id. The worker claims the run from
    `waiting`.
  - It rebuilds the graph's state from the steps already recorded: succeeded steps' outputs are
    in `workflow_run_steps`.
  - It resumes the waiting agent node with `Command(resume=...)` on that agent run's thread, then
    carries on through the graph as usual.
- **Run page.** It shows the waiting node, and links to the request with approve and reject
  buttons.

**Interactive agent runs** (the agent's chat) use the same interrupt:

- The turn ends with an approval card for the request, in place of the agent's reply.
- Approving or rejecting there, or on the Data page, resumes the thread. The rest of the turn
  streams in if the chat is open, or is there next time the run is opened.
- A new message sent while the run waits is refused until the request is decided.

**The documents chat** has no run and no thread, so it isn't given the tool.

**Runs stay on the versions they started on**, however long they wait. A workflow run resumes on
its pinned workflow version, and each agent node on its pinned agent version, even if either has
been edited meanwhile.

- The waiting run shows which of its pins are no longer current, so the owner knows what it will
  resume on.
- If a pin can no longer run when the run resumes, the step fails with that reason and the run
  fails as usual. That covers an agent that has since been deleted (its runs and threads go
  with it) or a tool that has left the library.
- If the run is deleted while it waits, its pending request becomes `cancelled`.

### Notifications

In the app only, for now. Email can be added later as another way to deliver the same rows.

- A `notifications` table in the platform database: `id`, `user_id`, `kind`, `ref` (jsonb, here
  the request id and store id), `created_at`, `read_at`.
- A new request creates one notification for each user who passes `can_approve(user, store)`.
  Today that's the owner; with teams, it's every approver.
- A decided, expired or cancelled request marks its notifications read.
- `GET /api/notifications` (unread first, with a count) is polled by the SPA, which shows a badge
  in the navigation. Clicking an item opens the request.
  `POST /api/notifications/{id}/read` marks one read.
- Notifications are a platform feature, not tied to schema changes, so other kinds (a workflow
  run that failed, a store over quota) can use them later.

### Destructive changes

Backups are per instance, so restoring one store's table means restoring a snapshot to a new
instance and copying it back. Drops are made reversible instead:

- `drop_table` and `drop_column` rename the table or column to `_dropped_<timestamp>_<name>` and
  hide it from `describe_data`.
- A scheduled task purges these after `DATA_STORE_DROPPED_RETENTION_DAYS`.
- Until then, the owner can restore them from the Data page.
- Dropped data still counts towards the quota until it's purged.

## Storage quota

- Each store has a cap made of two configuration values:
  - `DATA_STORE_DB_QUOTA_BYTES`: measured with `pg_database_size`
  - `DATA_STORE_FILES_QUOTA_BYTES`: the sum of `documents.size_bytes`
- A scheduled task records usage on `data_stores`. Uploads and `write_data` check it first.
- Over the cap, writes fail with a clear error, while reads and deletes keep working.
- Per-store overrides can come later (a nullable column per quota). The defaults live in
  configuration.

## Run history export (opt-in)

- A per-user setting, `export_run_history`. For a team store, later, it becomes a setting of the
  store.
- **Switching it on** runs the `history` migrations if they haven't run yet, and enqueues a
  backfill of the owner's existing runs.
- **Ongoing**:
  - When an agent turn ends or a workflow run finishes or pauses, the platform writes an outbox
    row in the same transaction as the run's own update. The row lives in `history_exports`
    (`data_store_id`, kind, source id, enqueued at, exported at).
  - An `aios.export_history` task drains the outbox and upserts into the store:
    - `history.agent_runs` and `history.messages` (flattened from the checkpoint: role, text, tool
      calls, usage)
    - `history.workflow_runs` and `history.workflow_run_steps`
  - Upserts are keyed on the platform's ids, so re-running is safe.
  - Each run row records the user who ran it.
- **Messages can also be embedded**, behind a setting, so agents can search past runs.
- **Switching it off** stops the export. What was already exported stays. The owner can delete it
  from the Data page, which empties the `history` tables.
- **Run deletes** delete the exported copy too, while the export is on.

## Configuration

New settings in `config.py`. Each has a default, and can be overridden from the environment.

| Setting | Purpose |
| --- | --- |
| `DATA_STORE_HOST`, `DATA_STORE_PORT` | Where new stores are created (defaults: the platform DB's) |
| `DATA_STORE_PROVISIONER_URL` | The provisioner's DSN (only the provisioning worker has it) |
| `DATA_STORE_CREDENTIALS_KEY` | Encrypts store passwords |
| `DATA_STORE_FILES_BUCKET` | The S3 bucket for raw uploads |
| `DATA_STORE_FILES_PUBLIC_ENDPOINT` | The S3 endpoint that presigned URLs use (RustFS's public address locally; unset on AWS) |
| `DOCUMENT_DOWNLOAD_URL_SECONDS` | How long a download link lasts (default 60) |
| `DATA_STORE_DB_QUOTA_BYTES`, `DATA_STORE_FILES_QUOTA_BYTES` | The storage cap |
| `DATA_STORE_DROPPED_RETENTION_DAYS` | How long dropped tables and columns are kept |
| `DATA_STORE_DELETED_RETENTION_DAYS` | How long a deleted user's store is kept before it's dropped |
| `DATA_STORE_STATEMENT_TIMEOUT`, `DATA_STORE_CONNECTION_LIMIT` | Per-role limits |
| `QUERY_DATA_MAX_ROWS`, `QUERY_DATA_MAX_BYTES`, `WRITE_DATA_MAX_ROWS` | Tool caps |
| `SCHEMA_CHANGE_REQUEST_EXPIRY_DAYS` | When an undecided request is treated as rejected |

## Infrastructure

- **RDS**: same instance.
  - Create `aios_app` and `aios_provisioner`, and stop the app connecting as the master user.
  - The master password stays in its secret, for break-glass use only.
  - Review `max_connections` against the expected store count and pool sizes.
- **S3**: a new bucket, `aios-data-<env>`.
  - SSE-KMS, Block Public Access on, and versioning off. Deletes must really delete, for user
    deletion.
  - The API's and worker's task roles may `GetObject`, `PutObject` and `DeleteObject` under
    `stores/*`.
  - The worker and migrate tasks currently share a role with no permissions, so the worker gets a
    role of its own.
  - A gateway VPC endpoint for S3 already exists for the sandboxes. The app subnets get a route to
    it too.
- **Secrets**:
  - `aios/provisioner`: given only to the provisioning worker.
  - `DATA_STORE_CREDENTIALS_KEY`: given to the API and the workers.
- **ECS**: a provisioning worker, or the existing worker with `-Q provisioning`, with its secrets.
- **Local**:
  - Compose's `pgvector/pgvector:pg17` container plays the same part. The provisioner uses its
    superuser.
  - RustFS (single node; MinIO is no longer maintained) stands in for S3. Only the S3 API is used, so any S3-compatible server would do.
- **Monitoring**:
  - Usage per store against its quota.
  - Slow queries per role.
  - Stores stuck behind on migrations.
  - Requests waiting a long time.

## Phases

Each phase ships on its own and leaves the app working.

1. **Data stores and provisioning**
   - The `aios_app` and `aios_provisioner` roles, the `data_stores` table and
     `aios/datastores/`.
   - The data store Alembic tree (`aios` schema: documents, chunks, schema_history).
   - Provisioning on sign-up, the fan-out migrator, and the isolation tests.
2. **Documents move**
   - The S3 bucket and RustFS.
   - Upload, ingest and search against the store, and downloads through presigned URLs.
   - `ChatDeps.data_store_id` and tasks that take `data_store_id`.
   - Drop `documents` and `chunks` from the platform database.
3. **Data tools**: `describe_data`, `query_data`, `write_data` and the quota checks.
4. **Schema change requests**
   - The spec and compiler, `request_schema_change` with `interrupt()`, the Data page, the
     applier, and reversible drops.
   - Paused and resumed agent runs and workflow runs.
   - In-app notifications.
5. **Run history export**: the setting, the outbox, the backfill, the export task and the `history`
   schema.
6. **Later**:
   - Teams owning data stores.
   - Per-store restore from nightly `pg_dump`s.
   - Per-store quota overrides.
   - A second RDS instance when one isn't enough.

## Testing

- **Isolation**: for two stores A and B, A's `rw` and `ro` roles can't connect to B or to the
  platform database, can't run `CREATE` anywhere, and `ro` can't write.
- **Provisioning**: the steps can be re-run, and a crash at each step can be resumed.
  Deprovisioning drops the database, the roles and the S3 prefix after the retention period.
- **Migrations**: the fan-out brings mixed-revision stores to head. One failing store doesn't
  stop the rest.
- **Documents**: an upload whose insert or enqueue fails leaves no S3 object. Ingest reads from
  S3.
- **Downloads**: another store's document is a 404. The link expires on time and downloads as an
  attachment with the original filename.
- **Schema changes**:
  - The compiler's output for each operation, with identifiers quoted.
  - Requests the current schema rejects, and stale requests.
  - A failed apply leaves the schema unchanged.
  - Drops are reversible until purged.
  - Expiry counts as a rejection.
- **Pausing**:
  - A workflow whose node requests a change ends up `waiting`, with its other branches finished,
    and no worker held.
  - Approving resumes it from the waiting node. Rejecting resumes it with the rejection.
  - The heartbeat check and the time limit ignore waiting time.
  - An interactive run resumes after a decision and refuses new messages until then.
  - A run resumes on its pinned versions after the agent or workflow has been edited.
  - A run whose agent was deleted while waiting fails on resume.
  - Deleting a waiting run cancels its request.
- **Notifications**: each approver gets one. Deciding, expiring or cancelling the request marks it
  read.
- **Tools**: row and byte caps, the statement timeout, read-only enforcement, embedding columns,
  and quota refusals.
- **Export**: outbox to store, idempotent re-export, backfill, deletes, and history kept after
  the export is switched off.
- **Fixtures**: `conftest.py` gains fixtures that provision throwaway stores (and RustFS prefixes)
  on the test servers, and drop them afterwards.

## Open questions

None at the moment.

## Decision log

- **Platform data stays on the platform.** Agents, skills, workflows and run history are platform
  data. Documents, chunks and agent-created tables live in the data store. A copy of run history
  goes there too, if the owner opts in.
- **Raw files go to S3** under the store's prefix. Postgres keeps metadata and chunks. Users
  download them through presigned URLs that last a minute.
- **Notifications are in the app only**, for now, in a general-purpose `notifications` table.
- **Runs always stay on the versions they started on**, including after a pause. A run whose
  pins can no longer run fails when it resumes.
- **We own the data store's schema.** Users don't run DDL, and never connect directly. Agents
  propose changes, the owner approves them, and the platform applies them. Runtime roles have no
  `CREATE`, so Postgres enforces this.
- **Workflow runs pause for approval**, holding no worker. Interactive runs use the same
  interrupt.
- **Only the store's owner approves**, through `can_approve`, which teams will extend.
- **Agents write only through `write_data`**, a structured tool. There's no raw DML.
- **History stays when the export is switched off.** The owner can delete it.
- **Quotas, retention periods and expiry are configuration values.**
- **One RDS instance, one database per store, for now.** `data_stores.host` and `port` keep a
  later move to a data copy.
- **Data stores are their own entity, keyed by UUID.** Users own them today; teams will too.
  Database, role and S3 names never contain a user id.
- **Structured schema specs, not SQL.** The platform compiles every change, so the approval
  screen shows exactly what will run.
- **No migration of existing data.** The platform is in development, and dev data is discarded.

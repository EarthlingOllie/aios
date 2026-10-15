# User databases: a Postgres database per user, for the data their agents gather

Status: **plan** (2026-10-15).

## Goal

Every user gets a Postgres database of their own. We provision it, and only the platform changes
its schema. It holds the data the user's agents and workflows gather for them: relational
tables and vector embeddings. Everything the platform needs to run (users, agents, skills,
workflows, runs, chat history) stays in the platform database.

- **Platform database**: the platform's own data. Every feature that needs data stores it here.
- **User database**: the user's data. Agents and workflows read and write it through tools. Its
  schema grows through **schema change requests**, which agents propose and the user approves.
- **Run history** can be copied into the user's database if they switch it on. That's a one-way
  export. The platform database stays the source of truth.

For now, all user databases sit on the existing RDS instance, one database per user. Each has its
own generated roles, so no user's credentials can reach another user's database or the platform's.
The platform records where each user database lives (host, port, name, roles), so moving some
users to another instance later is a data move, not a redesign.

## Architecture

```
                         ┌──────────────── RDS instance (Postgres 17 + pgvector) ────────────────┐
api ─────────────────────┼─▶ aios (platform DB)                                                   │
workflow-worker          │     users, agents, skills, workflows, *_versions, agent_runs,          │
worker                   │     workflow_runs, langgraph.* (checkpoints), sandbox_sessions,        │
  │                      │     user_databases, schema_change_requests, history_exports            │
  │                      │                                                                        │
  └── as u_<id>_rw ──────┼─▶ udb_<id> (one per user)                                              │
      (resolved per user)│     aios.*   platform-managed: documents, chunks, schema_history,      │
                         │              alembic_version                                           │
provisioner ─────────────┼─▶    data.*   agent-defined tables, changed only by approved requests   │
(CREATEDB, CREATEROLE)   │     history.* optional copy of the user's run history                  │
                         └────────────────────────────────────────────────────────────────────────┘
```

### What lives where

| Data | Database | Notes |
| --- | --- | --- |
| `users`, sessions, settings | Platform | |
| Agents, skills, workflows, their versions and nodes | Platform | Platform features |
| `agent_runs`, LangGraph checkpoints, `workflow_runs`, `workflow_run_steps` | Platform | Source of truth for run history |
| `sandbox_sessions` | Platform | Runtime state and secrets |
| `user_databases`, `schema_change_requests`, `history_exports` | Platform | New: the machinery that manages user databases |
| Uploaded documents and their chunks (text, embeddings, FTS) | User, `aios` schema | Moved from the platform DB. See open question 1 |
| Tables agents create for the user | User, `data` schema | Created and changed only by approved schema change requests |
| Run history copy | User, `history` schema | Only while the user has the export switched on |

### Three schemas in each user database

- **`aios`**: tables every user database has (documents, chunks, the schema change log). The
  platform owns them and migrates them with their own Alembic tree. Agents can read them and
  write them through platform tools, but never change their structure.
- **`data`**: the user's own tables. They're created and changed only through schema change
  requests. Alembic ignores this schema.
- **`history`**: the run history export, created when the user first switches it on. The
  platform owns its structure too (it's part of the user-DB Alembic tree).

## Provisioning and isolation

### Roles

The platform database's connections stop using the RDS master user:

- `aios_app`: the API and workers' role for the platform database. No `CREATEDB` or `CREATEROLE`.
- `aios_provisioner`: `CREATEDB` and `CREATEROLE`. Only the provisioning tasks use it, to create
  and drop user databases and roles and to apply schema changes. Its credentials are a separate
  secret, given only to the worker that runs those tasks.

Each user database `udb_<id>` gets three roles:

| Role | Login | Privileges |
| --- | --- | --- |
| `udb_<id>_owner` | No | Owns every schema and table in the database. The provisioner `SET ROLE`s to it to run migrations and approved changes. |
| `udb_<id>_rw` | Yes | `CONNECT`; `USAGE` on the schemas; `SELECT`, `INSERT`, `UPDATE`, `DELETE` on tables (via `ALTER DEFAULT PRIVILEGES FOR ROLE udb_<id>_owner`). No `CREATE` anywhere. |
| `udb_<id>_ro` | Yes | `CONNECT`; `USAGE`; `SELECT` only. Used to run the queries agents write. |

The login roles carry their own limits (`ALTER ROLE ... SET statement_timeout`,
`idle_in_transaction_session_timeout`, and `CONNECTION LIMIT`), so one user can't hold the
instance's connections or run unbounded queries.

Because the runtime roles can't run DDL, the rule that "only approved changes alter the schema"
is enforced by Postgres, not just by our code. A tool that passes model-written SQL through can,
at worst, read or change the user's own rows.

### Isolation on a shared instance

RDS doesn't let us edit `pg_hba.conf`, so isolation comes from privileges:

- `REVOKE CONNECT ... FROM PUBLIC` on every database: the platform's, `postgres`, `template1`
  and each user's. Then grant `CONNECT` only to that database's own roles.
- `REVOKE ALL ON SCHEMA public FROM PUBLIC` in each new database (Postgres 15+ already does this
  for `CREATE`), and drop `public` in user databases altogether.
- The catalogs `pg_database` and `pg_roles` are visible to every role, so database and role names
  are opaque ids (`udb_<id>`), never logins or emails.
- `pg_stat_activity` already hides other roles' query text from non-superusers.
- User databases get only `vector`: no `dblink`, `postgres_fdw` or untrusted languages.
- `template1` stays empty, since new databases copy it.

A test asserts each of these: one user's roles can't connect to another user's database or to
the platform's, and can't create anything.

### Credentials

`user_databases` (platform DB) has one row per user:

- `user_id`, `host`, `port`, `dbname`
- the role names, and the `rw` and `ro` passwords, encrypted with a key from Secrets Manager
  (`USER_DB_CREDENTIALS_KEY`; Fernet or KMS envelope encryption). Generated with `secrets`;
  never logged, never returned by the API.
- `status`: `provisioning`, `ready`, `failed`, `deleting`
- `schema_revision`: the user-DB Alembic revision it's at
- `created_at`, `error`

Rotation means `ALTER ROLE ... PASSWORD`, updating the row, then evicting cached engines.

### Lifecycle

- **Sign-up**: `auth_callback` creates the `user_databases` row as `provisioning` and enqueues
  `aios.provision_user_db` (its own queue). The task runs `CREATE DATABASE`, creates the roles,
  `CREATE EXTENSION vector`, revokes and grants as above, runs the user-DB migrations, then marks
  it `ready`. Each step can safely be re-run, so a retry after a crash finishes the job.
- **Until it's ready**: features that need it (uploads, search, data tools) answer
  "your database is being set up" (409), and the UI shows that state.
- **Deleting a user**: mark the row `deleting`, terminate its connections, then
  `DROP DATABASE ... WITH (FORCE)` and drop the roles, after a grace period (see open question
  7).
- **Moving instances later**: `pg_dump` / `pg_restore` the database, then update `host` and
  `port`. Nothing else refers to where it lives.

## Reaching a user's database from code

A new package, `aios/userdb/`:

- `connections.py`: `user_session(user_id, role="rw")` and an async equivalent. They look up
  `user_databases`, decrypt the credentials and open a session.
  - Engines are cached per (user, role) in a small LRU, with small pools
    (`pool_size=1, max_overflow=2`), and evicted when idle.
  - At this scale, that keeps the connection count bounded. PgBouncer or RDS Proxy can go in
    front later without changing callers.
- `provision.py`: create, migrate and drop user databases, run as `aios_provisioner`.
- `schema_changes.py`: the change spec, its compiler to SQL, and its application (below).
- `migrations/`: the user-DB Alembic tree (`aios` and `history` schemas). Its version table is
  `aios.alembic_version`, and an `include_name` hook excludes the `data` schema.

Changes to existing code:

- `db.py` keeps the platform engine (`SessionLocal`, `DB`). Nothing user-owned goes through it.
- **Documents and search**: `api/documents.py`, `search.py`, `ingest.py` and
  `tools/hybrid_search.py` use `user_session(user_id)`. `owner_id` goes from `documents`, since
  the database itself is the owner.
- **Tasks take the user**: `ingest_document_task(user_id, document_id)`. Every task that touches
  a user database is given the user explicitly.
- **Migrations**: `alembic/` stays the platform tree. A `aios.migrate_user_dbs` task (and a
  `deploy.sh` step after the platform migration) brings every `ready` user database to head and
  records its revision.
  - User-DB migrations must be backwards compatible (expand, then contract in a later release),
    because databases are upgraded one at a time while the new code is already running.
  - A failure stops that database, not the rollout, and is reported.

Two concerns from the earlier review no longer apply:

- Checkpoints stay in the platform database, so the per-process `_ready` flag in
  `checkpoints.py` is fine as it is.
- One instance means one pgvector and Postgres version everywhere.

## Agent tools for the user's data

New tools in the tool library, each with settings like any other (so agent versions pin them):

| Key | What it does | Runs as |
| --- | --- | --- |
| `describe_data` | Lists the tables in `data` (and readable `aios` tables), with columns, types and descriptions (`COMMENT ON`), read from the catalog | `ro` |
| `query_data` | Runs one read-only SQL statement. Results are capped in rows and bytes. | `ro`, in a `READ ONLY` transaction |
| `write_data` | Inserts, upserts, updates or deletes rows in a `data` table, described as structured arguments that the platform turns into SQL. Columns marked as embeddings are embedded on the way in. | `rw` |
| `request_schema_change` | Proposes a schema change (below). Returns the request's id and status. | Nothing: it only records the request |

- What these tools return is data, not instructions. Stored rows can carry prompt injection like
  any tool result, and the docstrings say so.
- `run_python` gets no database credentials. If code in the sandbox needs data, the agent queries
  it and passes it in. A scoped, short-lived route from the sandbox can come later.

## Schema change requests

### What an agent can ask for

A request is a list of operations in a structured spec (a Pydantic model), never SQL:

- `create_table`: name, description, columns, primary key (a `bigint` identity `id` by default)
- `add_column`: table, name, type, nullable, default, description
- `create_index`: btree, GIN for `jsonb`/FTS, or HNSW for a vector column
- `drop_column`, `drop_table`: destructive, see below
- maybe later: `rename_column`, `alter_column_type`

Column types come from an allowlist: `text`, `integer`, `bigint`, `numeric`, `boolean`,
`date`, `timestamptz`, `jsonb`, `uuid` and `vector(n)`. There's also an `embedding` type: a
`vector(EMBEDDING_DIM)` column tied to a text column, which `write_data` embeds with the
platform's embedding model, so agents never have to produce vectors themselves.

Names must match `^[a-z][a-z0-9_]{0,62}$` and aren't reserved words. Every identifier is quoted
by the compiler. Operations only ever touch the `data` schema.

### Flow

1. **Request.** The tool validates the spec against the database's current schema: a new table
   must not already exist, a column must exist before it can be dropped, and so on. It then
   compiles the spec to SQL and stores a row in `schema_change_requests` (platform DB):
   - `id`, `user_id`, `ops` (jsonb), `sql` (the compiled preview), `reason` (the agent's
     explanation)
   - where it came from: `agent_run_id`, and the workflow run and step if any
   - `base_revision`: the user database's schema revision when it was made
   - `status`: `pending`, `approved`, `rejected`, `applied`, `failed`, `stale`
   - `decided_by`, `decided_at`, `applied_at`, `error`
2. **Review.** A *Data* page lists pending requests with the agent's reason and the exact SQL.
   The user approves or rejects each one. Destructive operations show what they would remove
   (row counts) and need the table's name typed to confirm.
3. **Apply.** On approval, `aios.apply_schema_change` (provisioner queue) runs:
   - It connects as `aios_provisioner`, `SET ROLE udb_<id>_owner`, and runs the SQL in one
     transaction. Postgres DDL is transactional, so a request applies fully or not at all.
   - It sets `lock_timeout` so it can't stall the user's own queries for long.
   - It appends a row to `aios.schema_history` in the user's database and bumps the revision.
   - If the schema has changed since `base_revision`, it validates the request again. A request
     that no longer applies is marked `stale` instead of failing halfway.
4. **The agent finds out.**
   - In this first version, `request_schema_change` returns at once with "pending approval"; the
     agent says so and carries on, or ends its turn. A later turn or run sees the new table
     through `describe_data`.
   - Pausing the run until approval (LangGraph `interrupt()`, resumed by the approval) is a later
     phase. It needs a "waiting for approval" state on agent runs and workflow runs that the
     heartbeat and time limits allow for (see open question 3).

### Destructive changes

Backups are per instance. Restoring one user's table means restoring a snapshot to a new
instance and copying the table back, so drops are made reversible:

- `drop_table` and `drop_column` rename the table or column to `_dropped_<timestamp>_<name>` and
  hide it from `describe_data`.
- A scheduled task purges these after a retention period (say 30 days).
- Until then, the user can restore them from the Data page.

## Run history export (opt-in)

- A per-user setting, `export_run_history`, on the platform side (a `user_settings` row or a
  column on `users`).
- **Switching it on** runs the `history` migrations if they haven't run yet, and enqueues a
  backfill of the user's existing runs.
- **Ongoing**:
  - When an agent turn ends or a workflow run finishes, the platform writes an outbox row
    (`history_exports`: user, kind, source id, enqueued at, exported at). It's written in the
    same transaction as the run's own update, so nothing is missed.
  - An `aios.export_history` task drains the outbox and upserts into the user database:
    `history.agent_runs`, `history.messages` (flattened from the checkpoint: role, text, tool
    calls, usage), `history.workflow_runs` and `history.workflow_run_steps`.
  - Upserts are keyed on the platform's ids, so re-running is safe.
- **Messages can be embedded too**, behind a setting, so agents can search past runs with the same
  hybrid search as documents.
- **Switching it off** stops the export. Whether the copy already exported is kept is open
  question 5.
- **Run deletes** also delete the exported copy, while the export is on.

## Moving existing data

For each existing user:

1. Provision their database.
2. Copy their `documents` and `chunks` rows across. Embeddings copy as they are: same model,
   same size, no re-embedding.
3. Check the counts match.
4. Switch reads and writes to the user database (a per-user flag on `user_databases` while both
   paths exist).
5. Once every user is moved, a platform migration drops `documents` and `chunks` from the
   platform database.

## Infrastructure

- **RDS**: same instance.
  - Create `aios_app` and `aios_provisioner`, and stop the app connecting as the master user.
  - The master password stays in its secret for break-glass use only.
  - Review `max_connections` for the expected user count and pool sizes.
- **Secrets**:
  - `aios/provisioner` (DSN): given only to the provisioning worker.
  - `USER_DB_CREDENTIALS_KEY`: given to the API and the workers.
- **ECS**: a `provisioner-worker` service, or the existing worker with `-Q provisioning`, plus the
  matching task definition secrets.
- **Network**: no change. User databases are on the instance the app already reaches.
- **Local**: Compose's `pgvector/pgvector:pg17` container plays the same part. The provisioner
  uses its superuser.
- **Monitoring**:
  - `pg_database_size` per user database, for a quota (Postgres has no per-database storage
    limit, so the platform enforces it: writes fail once a user is over).
  - Slow queries per role.
  - Users whose database is stuck behind on migrations.

## Phases

Each phase ships on its own and leaves the app working.

1. **Groundwork, no behaviour change**
   - Split the `aios_app` and `aios_provisioner` roles.
   - Add the `user_databases` table and `aios/userdb/connections.py`.
   - Make document, search and ingest code go through `user_session()`, which still returns the
     platform database for now.
   - Celery tasks carry `user_id`.
2. **Provisioning**
   - The user-DB Alembic tree (`aios` schema: documents, chunks, schema_history).
   - Provisioning on sign-up, the fan-out migrator, and the isolation tests.
   - Move existing documents and chunks, then drop them from the platform DB.
3. **Data tools**: `describe_data`, `query_data` and `write_data`, against tables created by hand
   in tests.
4. **Schema change requests**: the spec and compiler, `request_schema_change`, the Data page with
   approve and reject, the applier, and reversible drops.
5. **Run history export**: the setting, outbox, backfill, export task, and the `history`
   schema.
6. **Later**:
   - Pausing runs for approval.
   - Direct, read-only access for users (BI tools, `psql`), which needs a public endpoint or a
     proxy and its own threat model.
   - Per-user restore from nightly `pg_dump`s.
   - A second instance when one isn't enough.

## Testing

- **Isolation**: for two provisioned users A and B, A's `rw` and `ro` roles can't connect to B's
  database or the platform's, can't run `CREATE` in any schema, and `ro` can't write.
- **Provisioning**: the steps can be re-run, and a crash at each step can be resumed.
  Deprovisioning drops everything.
- **Migrations**: the fan-out brings mixed-revision databases to head. One failing database
  doesn't stop the rest.
- **Schema changes**:
  - The compiler's output for each operation, with identifiers quoted.
  - Requests the current schema rejects, and stale requests.
  - A failed apply leaves the schema unchanged.
  - Drops are reversible until purged.
- **Tools**: row and byte caps, statement timeout, read-only enforcement, embedding columns.
- **Export**: outbox to user database, idempotent re-export, backfill, delete propagation.
- **Fixtures**: `conftest.py` gains a fixture that provisions throwaway user databases on the test
  server and drops them afterwards.

## Open questions

1. **Uploaded documents.** This plan moves `documents` and `chunks` into the user database: they
   are the user's content and embeddings. But document upload is also a platform feature, which
   your rule would put on the platform side. Which is it?
2. **Raw upload bytes.** Keep them in the user database (as today, in `documents.data`), or move
   them to S3 under a per-user prefix and keep only metadata and chunks in Postgres?
3. **Waiting for approval.** Is "request, then carry on" enough at first? Or should runs, and
   especially workflow runs, pause until the user decides?
4. **Who approves.** Only the user who owns the database for now. If accounts become shared
   (teams), approval rights need a model of their own.
5. **Switching the history export off.** Keep what was already exported (it's the user's copy), or
   delete it?
6. **Writes from agents.** Structured `write_data` only, or also a raw DML tool (`INSERT` /
   `UPDATE` / `DELETE` SQL as `rw`)? The roles make the raw tool safe from other users, but not
   from the agent damaging the user's own data.
7. **Quotas and retention.** A storage cap per user, how long dropped tables and columns are kept,
   and how long a deleted user's database is kept before it's dropped.
8. **Direct access.** Will users ever connect to their database themselves? It changes the
   network design (today RDS is private) and makes the `ro` role user-facing.

## Decision log

- **Platform data stays on the platform.** Agents, skills, workflows and run history are platform
  data. Only what agents and workflows gather for the user (and, if they opt in, a copy of their
  run history) goes to the user database.
- **We own the user schema.** Users don't run DDL. Agents propose changes, a person approves
  them, and the platform applies them. Runtime roles have no `CREATE`, so this is enforced by
  Postgres.
- **One RDS instance, one database per user, for now.** It's cheap and simple at the expected
  scale. `user_databases.host` and `port` keep a later move to more instances to a data copy.
- **Structured schema specs, not SQL.** The platform compiles every change, so the approval
  screen shows exactly what will run and identifiers are always quoted.

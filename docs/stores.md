# Stores

Each organisation has a **store**: a Postgres database of its own, on the same instance as the
platform database, which holds its agents, skills and workflows, their runs and the runs'
history, its schedules, its chats with their members, messages and invocations, its agents'
schema change requests, its documents, the data tables its agents asked for, and, when it's
switched on, a copy of its run history. The platform database holds no organisation content:
only identity, organisations, memberships, invitations, the store registry (with its credentials
and the run history export's outbox), notifications, and the schedule due index. In code and on screen it's a "data store"
(`aios.datastores`, the `data_stores` table, the "Data store" notices); screens show its
contents as **Data**. See also [Accounts and organisations](accounts-and-organisations.md) and
[Tools](tools.md), for the tools that read and write it.

## The Data page

The **Data** page shows the organisation's store, the same for every member: its status
(Overview), the schema changes its agents asked for (Changes: those waiting for a decision, then
the history), the tables and columns they dropped (Dropped: restorable until purged), its data
tables (Tables, below), and its documents, with each one's ingest status, chunk count, size,
upload time and who uploaded it, upload, download and delete (the chat's documents panel in the
Chats section uploads to the same list). Each section is a tab with its own URL
(`/data/documents`).

Documents are parsed and chunked with Docling (`HybridChunker`), embedded with Voyage, and
stored in the store, where the hybrid search finds them (see [Tools](tools.md#search)). The
ingest runs on the worker (`aios.ingest`: Docling convert, HybridChunker, embed, store).

## Database roles

Nothing in the app connects to Postgres as the master user (`aios`, RDS's or Compose's):

| Role | Attributes | Used by |
| --- | --- | --- |
| `aios_app` | `LOGIN`; no `CREATEDB` or `CREATEROLE` | the API, the workers and migrations (`DATABASE_URL`). It owns the platform schema (`public` and everything in it), so it can migrate it, and may create schemas (it did for the checkpointer's `langgraph`, which is in each store now). It doesn't own the database, so it can't drop or reconfigure it. |
| `aios_provisioner` | `LOGIN CREATEDB CREATEROLE` | creating and migrating stores (`DATA_STORE_PROVISIONER_URL`). On AWS its secret is for the provisioning worker and the migrate-stores task only. |

`python -m aios.db_roles` creates them as the master user (`DATABASE_ADMIN_URL`, which nothing
else reads), taking each role's name and password from its URL. It also creates the `vector`
extension (not one a non-superuser can create), grants `aios_app` `CREATE` on the platform
database, and makes it own `public` and anything else in the database the master user still
owns, so a database that was migrated as the master user is handed over; the master user stays
a member of `aios_app`, for break-glass access. It's safe to re-run: to change a password,
change the URL and run it again.

It changes nothing (it runs in one transaction) and exits with an error if: the server is older
than PostgreSQL 16 (before 16, `CREATEROLE` reaches roles it didn't create); the three URLs name
fewer than three roles, or different hosts or ports (compared as written, so not `localhost` in
one and `127.0.0.1` in another); `aios_app` has, or may `SET ROLE` to a role that has,
`SUPERUSER`, `CREATEDB`, `CREATEROLE`, `BYPASSRLS` or `REPLICATION` (on RDS, don't grant it
`rds_superuser`); or the master user would still own something in the database it can't hand
over, which it lists. Compose runs it as the `db-roles` service, and `infra/deploy.sh` as the
`db-roles` task before every migration (see [Deploy](deploy.md)). The master user is otherwise
for break-glass access only.

It also revokes `CONNECT` from `PUBLIC` on the platform database (then grants it to the two
roles), `postgres` and `template1`, so no other role (a store's included) can connect to
them. Only a database's owner may do that, so one the master user doesn't own is left as it is
and reported as a warning. And it makes the stores' template, `aios_store_template`
(`DATA_STORE_TEMPLATE`): an empty database with pgvector and pgstattuple (for measuring a
store's data) in an `extensions` schema, which no one may connect to and the provisioner copies.
It adds pgstattuple to any store made before the template had it. Stores it may not connect to,
or create it in (on RDS, the master user isn't a superuser), are listed in one warning; any of
them without it is measured by its database's size until a superuser adds it.

## Data stores

Each organisation has a store, which every member reaches: the Data page, uploads and
downloads, approvals and the run history export all resolve the signed-in user's
organisation's store (`aios.datastores.resolve.data_store_for`, the `CurrentDataStore`
dependency), and a run uses its agent's or workflow's organisation's. A user in another
organisation finds none of it (404). Each document records who uploaded it (`uploaded_by`, a
user id: a store has no users table). Signing up makes the new organisation's store's row
(`data_stores`, `owner_organisation_id`, status `provisioning`) and queues
`aios.provision_data_store` on the `provisioning` queue, which only the provisioning worker
reads. That task, as `aios_provisioner`:

- creates the roles `ds_<id>_owner` (no login), `ds_<id>_rw` and `ds_<id>_ro`, named after the
  store's id (a UUID, never its organisation's), with a connection limit each
  (`DATA_STORE_CONNECTION_LIMIT`), and a default statement timeout and idle-in-transaction
  timeout (`DATA_STORE_STATEMENT_TIMEOUT`, `DATA_STORE_IDLE_IN_TRANSACTION_TIMEOUT`, in
  milliseconds): role defaults, which a session may `SET` otherwise, so code running queries it
  doesn't control sets its own;
- creates the database `ds_<id>` from the template, owned by `_owner` and taking no
  connections, revokes `CONNECT` (and `TEMPORARY`) from `PUBLIC`, grants `CONNECT` to `_rw` and
  `_ro`, and only then lets them connect;
- drops its `public` schema, and sets default privileges: `_rw` may read and write the tables
  `_owner` creates and `_ro` only read them; neither may create anything;
- runs the store Alembic tree (`aios.datastores.migrations`, version table
  `aios.alembic_version`) as `_owner`, takes writes on the tree's bookkeeping
  (`aios.alembic_version`, `aios.schema_history`) back from `_rw`, and marks the store `ready`
  at its head revision. The tree makes the `aios` schema (the platform's tables) and an empty
  `data` schema (the organisation's tables, which the tree otherwise leaves alone).

Every step checks what's already there, so a run that crashed is finished by running it again.
After six failed tries (the first and five retries, over about half an hour) the store is marked
`failed`. The provisioning worker's beat (`-B`) tries `failed` stores again every ten minutes,
along with any still `provisioning` five minutes after they were made (their task was lost with a
worker, say). Until a store is `ready`, uploads, the document list and search answer 409 ("being
set up"), and the UI says so.

A store whose organisation is deleted (an empty one, on accepting an invitation) goes `deleting`,
with no organisation, in the same transaction. The provisioning worker's sweep
(`aios.delete_data_stores`, queued then, and on the beat every ten minutes) disposes of its own
engines on the store, drops its database and roles (ending any sessions still on it), deletes its
files under `stores/<id>/` and marks it `deleted`, keeping the row. Each step can be run again,
so a sweep cut short is finished by the next. A store still being provisioned (its creator
accepted an invitation straight after signing up) is never marked ready: the sweep waits for the
provisioning run, which leaves it `deleting`.

The login roles' passwords are generated with the row, stored encrypted with a key derived from
`DATA_STORE_CREDENTIALS_KEY`, and sent to Postgres only as SCRAM verifiers, so the app's logs
never have them, and a server logging statements has only the encrypted ones (in the
`data_stores` insert), useless without the key. The API returns only a store's status and
usage (`GET /api/o/{org}/data-store`).

`python -m aios.datastores.provision migrate-all` (Compose's `migrate-stores`, and a deploy step
after the platform's migration) finishes provisioning any store left `provisioning` or `failed`,
then brings every ready store to the store tree's head, one store at a time. A store that fails
keeps its revision, gets the error on its row, and is reported; the rest carry on, and the
command exits non-zero. Store migrations run while the previous release is still serving, so
each must work with the code before it (expand now, contract in a later release). The code,
though, needs its own head: a ready store behind it (the previous release made it after
migrate-all ran, or migrating it failed) answers 409 ("being upgraded") and holds its ingests
back, until the provisioning worker's beat brings it to head with the retries above.

Documents and chunks live in each store, in its `aios` schema (`aios.datastores.models`, made
by the store tree); the platform database has none. `store_session(id, role)` opens a session
on the store's own database: uploads, listing, deletion and ingest as `_rw`, search (the API's
and the document search tools') and downloads as `_ro`. Searches have no organisation filter: a
store holds only its organisation's documents, and its roles can't connect to any other
database. A store is reached at `DATABASE_URL`'s address (and with its options, like
`sslmode`): stores are on the platform database's instance, which the provisioner may know by
another name (Compose's `db`, with the API on the host). A row's `host` and `port` are for a
store moved to another instance later; they're empty until then.

**Stores are reached through a connection pooler** (PgBouncer, in transaction mode) at
`DATA_STORE_POOLER_ADDRESS` (Compose's `pgbouncer`, CI's service container, the `pooler` ECS
service on AWS; empty means directly). It may hand each transaction a different server
connection, so store connections carry no session state: psycopg prepares no statements, nothing
is set at connection startup (statement timeouts are the roles' defaults), and nothing takes a
session advisory lock (a `query_data` statement that does is let go of before its transaction
ends). The runs' checkpointers connect through it too (`store_conninfo`). The platform database
and the provisioner stay directly connected.
Sizing: [Deploy](deploy.md#connection-pooler).

Each process pools one connection per store and role, plus four more while busy. The pooler's
pools bound what reaches a store; each role's `DATA_STORE_CONNECTION_LIMIT` (default 100) is a
backstop behind them, for connections that skip the pooler. A connection past it is refused:
the API answers 503 to try again, the ingest task retries, and the search and data tools tell the
model. Through the pooler, a client past a store's pool waits for a server connection instead.

### One store per organisation

**Organisation data goes through its store** (`aios.datastores`, the `data_stores` table): one
per organisation, with an id of its own, made with the organisation (on the welcome page, or
from the switcher's "Create an organisation"). Document upload, listing,
deletion, search, ingest and the document search tools open their sessions with
`store_session(data_store_id, role)`; the run context (`ChatDeps`) and the ingest task carry
the `data_store_id`, so a run's tools act on its organisation's store whoever started it. Each store is a database of its own, so
keeping organisations' documents apart is the database's job, not a filter in every query.

### The people mirror

Each store has a **people mirror**, `aios.people` (`aios.datastores.people`): one row per person
who is or was a member of its organisation, with their id (their platform user id), first and
last names, email, role (admin for the creator, editor for everyone else, until roles exist), and
when they joined and left (`left_at`, null while they're a member). The platform is its source
of truth, and writes it after its own commit: joining, leaving, a rename or a new email, and a
role change mark the organisation's mirror as changed on the platform's session
(`people.changed`), and once that commits it's brought to match the memberships
(`people.refresh`), as the store's `_rw`, under a lock so the last to write read the latest. A
store not ready yet is filled by its provisioning (a step before it's marked ready, as the
owner), and the store migration command fills an existing store's as it brings it to the
revision that makes the table (`0008`). A write that fails is logged and left to the **repair
sweep** (`aios.repair_stores`, on the provisioning worker's beat every ten minutes), which brings
every ready store's mirror to match its organisation's memberships and logs and returns what it
fixed: rows it added (one deleted, or never written), updated (stale) and marked left.

"Made by" and "changed by" resolve through it (`people.resolve`, for `aios.api.provenance`): a
person's name, and whether they're a former member, from the mirror; anyone it doesn't have
(the store isn't ready or can't be reached, or it hasn't caught up) from the platform.

### Agents, skills and workflows

What the organisation builds is in its store (store revision `0009`, `aios.datastores.models`):
`aios.skills`, `aios.agents` and `aios.workflows`, each with its versions (`skill_versions`,
`agent_versions`, `workflow_versions`), the skill versions each agent version pins
(`agent_version_skills`), its tools and their settings (`agent_version_tools`), and each
workflow version's nodes and edges (`workflow_nodes`, `workflow_edges`). A workflow's `layout`
is on its row. Who made, changed, archived or saved each is a platform user id, resolved through
the people mirror; there's no foreign key, as a store has no users table. They're archived,
never deleted, so the versions pinned by other versions are always there.

**Ids are the store's own**, so they repeat across organisations: two organisations' first
agents both have id 1. Every agents, skills and workflows endpoint works in the asking
organisation's store (`aios.auth.CurrentStore`, a session on it for the request), so an id never
reaches another organisation's row: it names that organisation's own, or nothing. What's still
on the platform and names one of them (schedules, chat members, chat messages' authors and chat
invocations) keeps its id without a foreign key, beside the organisation it's in
(`organisation_id` on schedules and chats), and every lookup by it is within that organisation. Reading their names or version numbers for those goes to the store
(`aios.stored`).

**Writes across the two databases** follow the source of truth: the store commits first. Saving
an agent, skill or workflow writes only the store. Archiving one commits the store, then pauses
its schedules on the platform. Starting a run, adding one to a chat or saving a schedule for it
share-locks it in the store (so an archive waits), commits the platform's rows, and only then
ends the store's transaction, so an archive racing it either waits and then sees what it made,
or wins and is seen by it. The documents assistant (`aios.assistant`) is made in the store under
a lock for its transaction only, and the chat that gets it is committed after.

The platform's tables for them went in the same change (platform revision `0060`); nothing was
copied across, and development data is rebuilt with `make seed`.

### Runs

Agent runs (`aios.agent_runs`, each holding its turn lease), the sandbox each one's Python runs
in (`aios.sandbox_sessions`), workflow runs (`aios.workflow_runs`) and their steps
(`aios.workflow_run_steps`) are in the store too (store revision `0010`), as is each run's
history: the store's own checkpointer's tables, in its `langgraph` schema (see
[Agents and skills](agents-and-skills.md#runs)). Runs name the agent or workflow and the version
they ran by foreign key; who started them is a platform user id, with no foreign key. The
platform's tables for them, and its checkpointer schema, went in the same change (platform
revision `0062`); nothing was copied.

Schedules (`aios.triggers`), chats (`aios.chats`, `aios.chat_members`, `aios.chat_messages`),
invocations (`aios.invocations`: chat invocations became the store's general invocations, with
a cause, so far always a mention) and schema change requests (`aios.schema_change_requests`)
followed (store revision `0012`, platform revision `0063`, which drops the platform's tables;
nothing was copied, and development data is rebuilt with `make seed`). Chats moved as they
were, owner and visibility included. Beside the runs they name, they do so by foreign key: a
run deleted clears a schedule's last run, an invocation's run and a request's run; a schedule
deleted clears its runs' `trigger_id`; a waiting step names its request.

Every run then got an invocation, and its turns became records (store revision `0013`). A run
started by hand has a `manual` invocation, a schedule's a `schedule` one naming its trigger, and
a workflow agent node's is a child of its workflow run's (`parent_id`, with the chain of its
ancestors' ids and its depth), taking its cause (`aios.invocations.for_run`). Each turn of an
agent run is a row of `aios.turns`: its run, invocation, agent version, status and error, times,
and model calls, tool calls and tokens (`aios.turns`). A run's status, times and counters are
derived from its turns (its latest turn's status and times, the counters summed), so the run's
own columns for them were dropped. The run page lists the turns.

The `reporting` schema holds read-only views for agents to query with `query_data`:
`reporting.runs` (agent and workflow runs: kind, ids, cause, status, times and summed counters)
and `reporting.turns` (each turn's ids, cause, status, times, duration and counters). They hold
identifiers, counts, times and statuses only, never text: no messages, titles, inputs, outputs
or errors. Both roles read them through the store's default privileges; neither may write
through them (`upgrade_store` revokes it each migration).

- **Named by store and id.** Ids repeat across stores, so everything outside the store that
  names a row of it carries the store too: worker task arguments (`run_workflow_task`,
  `resume_agent_run_task`, `apply_schema_change_task`), the turn lease's Redis channels, a
  sandbox's session key, the firing of a schedule or an invocation, the schedule due index and
  notifications (a request's id and its store's). Trace sessions are
  `agent-run-<organisation>-<id>`, and a chat's handoffs `chat-<organisation>-<id>`.
- **Writes across the two databases.** The store is the source of truth for everything it holds
  and commits first; the platform's derived copies are written after, idempotently: the people
  mirror the other way round (see [Accounts and organisations](accounts-and-organisations.md)),
  a schedule's due index row (`aios.schedules`, see [Triggers](triggers.md#the-scheduler)), a
  request's notifications (made once it's recorded, cleared once it's decided, cancelled or
  expired), and a run's outbox row. A run's schema change requests are recorded holding its
  key share and cancelled under its lock (`schema_changes.lock_run`), both in the store, so none
  is recorded for a run that's gone. Leaving an organisation takes the leaver out of its chats
  in a store transaction that commits as the leave does (`organisations.leave_chats`).

### Sources

What brings content into the store is a source (the store's `aios.sources`, store revision
`0011`; module `aios.sources`): its kind, name, settings (never secrets), status and sync
bookkeeping. Every store has exactly one upload source, Uploads, made by that revision (so with
the store, and with `make seed` for development stores); a partial unique index keeps it the
only one. Every document belongs to a source (`documents.source_id`, required): uploads to
Uploads. Uploads can't be paused, changed or removed (409).

### The deny list

A few columns are left out of the read-only role's grants (`provision.DENIED_COLUMNS`), and so
out of `query_data`, `describe_data` and the Tables view: people's emails in the people mirror
and a run's sandbox's bearer token (`sandbox_sessions.secret`), for now (later: audit log
details, other secrets and credentials). The role keeps the table's other
columns. It's a few grant lines, applied every time a store is migrated (the default
privileges would otherwise give a new table's every column).

`write_data` writes as the store's **writer** role, `ds_<id>_writer` (no login): `_rw` may
`SET ROLE` to it but doesn't inherit its privileges, and the tool does so for each call's
transaction (`SET LOCAL ROLE`, so nothing outlives it on a pooled connection). The writer may
read, insert, update and delete in `data` (dropped tables and columns aside, as `_rw`), read the
bookkeeping it plans with (`aios.embedding_columns`, `aios.dropped_objects`) and nothing else:
it writes only in the `data` schema, and can't read the deny list either. Provisioning creates
it, and migrating a store made before it creates it and gives it its grants.

### Uploaded files

**Uploaded files are in S3** (`aios.datastores.files`; RustFS locally, in Compose and the
tests), at `stores/<store id>/documents/<document id>`; the store's `documents` row keeps the
key, the size and the SHA-256. An upload streams the file to S3, then inserts the row, then
queues the ingest, and deletes the object again if either of those fails (unless the row is
still there); the ingest reads it back from S3 (when S3 can't answer for now, the document
waits, and the task tries again a minute later, up to ten times), and deleting a document
deletes its object, then its row (so should S3 refuse, the document stays to be deleted
again). Downloading (`GET /api/o/{org}/documents/{id}/download`) looks the document up in the
organisation's store, so another's is a 404, and redirects to a presigned `GetObject` link that
saves it under its original name and lasts `DOCUMENT_DOWNLOAD_URL_SECONDS` (60). Anyone holding
a link can use it until then, so one is minted per click and never shown on the page. Links are
signed for `DATA_STORE_FILES_PUBLIC_ENDPOINT` when set (RustFS as the browser reaches it, while
the app reaches it at `DATA_STORE_FILES_ENDPOINT`). On AWS the bucket
(`<name>-data-<environment>`) has SSE-KMS, Block Public Access and no versioning (a delete
really deletes); only the API's and the ingest worker's task roles may get, put and delete
objects, under `stores/*`, and the app subnets reach it through an S3 gateway endpoint.

### Tables

The Data page's **Tables** (`GET /api/o/{org}/data/tables`, `.../tables/{table}/rows` and
`.../tables/{table}/value`, in `aios.api.data` and `aios.datastores.tables`) show the store's
`data` tables, with their columns and row counts (the planner's estimate for a big table), and
their rows a page at a time. They read as `_ro` through `aios.datastores.reads`, as
`query_data` does, so under the same statement timeout and row and byte caps. No SQL is taken:
a table, a sort column or a value's column is a name looked up in the catalog (404 or 422 if
it isn't there), and only then quoted into a statement. Values come as text, cut to 200
characters (read in full, as far as the byte cap, on asking); embeddings come as their type and
dimension (`vector(1024)`), never their numbers, and `jsonb` pretty-printed.

New tables and columns come from agents' schema change requests (see
[Tools](tools.md#request-schema-changes)).

### Quotas

Each store has a storage cap (`aios.datastores.quota`): `DATA_STORE_DB_QUOTA_BYTES` (default
1 GiB) for the data its tables hold, and `DATA_STORE_FILES_QUOTA_BYTES` (default 5 GiB) for its
documents' files, the sum of their `size_bytes`. The data is each table's live rows, as
`pgstattuple_approx` estimates them, in its own pages and its TOAST table (where large values
go), plus its indexes in proportion to how much of the table is live rows. Not the database's
size (`pg_database_size`): that keeps the space deleted rows took until their table is
rewritten, so deleting rows wouldn't bring a store back under its cap. Over either, uploads
answer 507 (as does an upload that wouldn't fit) and `write_data` refuses inserts, upserts and
updates, telling the model; reads, searches, downloads and deletes carry on. The checks measure
the store as the write comes in. What each store uses is also recorded on its `data_stores` row
for the Data page's Overview, which shows both against their caps, warning from 80%: the
provisioning worker's beat measures every ready store every five minutes, as its `_ro` role,
and uploads and deletes record the documents' bytes straight away. Deleting documents or rows
makes room at once; the Overview shows rows' room at the next measurement. The checks first read
the database's size, which is never less than the data, and only measure the data once that
size reaches the cap. Deleting rows doesn't bring the size back down, so from then on each check
measures: `pgstattuple_approx` reads the pages vacuum hasn't marked all-visible, the ones
written or deleted from since.

### Dropped tables and columns

**Drops are reversible** (`aios.datastores.dropped`). `drop_table` and `drop_column` rename
the table or column to `_dropped_<timestamp>_<name>` and record it in
`aios.dropped_objects`; applying one takes the login roles' access to it away (a table with
dropped columns keeps its privileges on its other columns only, so `SELECT *` on it is
refused until they're restored or purged), so it's gone from `describe_data`, `query_data`
and `write_data` while its data stays. The card and the Changes section show what a drop
would remove (row counts, the columns affected), and approving it takes the tables' names
typed (refused by the server otherwise). The Data page's Dropped section lists what's
dropped with when it's purged, and restores it (on the provisioning worker,
`aios.restore_dropped`), refused if its name has been taken since. The provisioning worker's
beat purges what's older than `DATA_STORE_DROPPED_RETENTION_DAYS` (30) every hour
(`aios.purge_dropped`). Restores and purges are logged in `aios.schema_history` too.

## Run history export

On the Organisation page's Settings tab (`/organisation/settings`) any member can switch on
`export_run_history` (on `data_stores`): a one-way copy of the organisation's runs (every
member's and every schedule's, from the store's `aios` schema) into its `history` schema, keyed
by the runs' ids and recording who
started each (`user_id`, the run's `created_by`: null for a schedule's run). Agent runs go in
`history.agent_runs`, with `history.messages` (role, text, tool calls and usage, flattened from
the run's checkpoint). Workflow runs go in `history.workflow_runs`, with
`history.workflow_run_steps`: every step at every depth, with its path, kind, status, input,
output, error and timings, and its agent run (a `history.agent_runs` id, not a foreign key: the
two are exported apart). Statuses are copied as text. The runs stay the source of truth
(`aios.datastores.history`):

- The `history` tables are the store tree's second line (branch label `history`, revisions
  `h0001`, ...), which a store gets only once the export is first switched on:
  `aios.set_up_history_export`, on the `provisioning` queue, runs it, and from then on every
  migration keeps it at its head (`data_stores.history_revision`). The stores' roles get the
  same default privileges on it as on `aios`. A store behind the line's head isn't exported
  until it's brought there.
- Switching it on backfills: every run in the organisation, agent and workflow runs alike, goes
  in the outbox, `history_exports` (a row per run, by kind, and store). Every agent turn ends in
  `_end_turn` (chats, schedules and workflow agent nodes alike, failed or stopped too), and
  every workflow run in `aios.workflows._end_runs` (finished, failed or stopped), each writing
  the run's row in the same transaction as the run's own update. Deleting a run, its agent or
  its workflow run writes it too, and deleting an agent writes the workflow runs whose steps
  held its runs. A workflow run that pauses (goes `waiting` on a schema change request) or
  resumes isn't written then: it's written when it ends, like any other.
- `aios.export_history` (default queue: it writes as the store's `_rw`) drains a store's outbox,
  upserting each run with its messages or steps, or deleting the copy of a run that's gone.
  It's queued when the outbox is written, and the provisioning worker's beat queues it every
  minute for any store with rows still pending. A run that fails to export keeps its error on
  its outbox row and is tried again at its next change, or in an hour, so it doesn't hold up
  the rest.
- Switching it off stops the export (after the batch under way) and keeps the copy; runs
  deleted meanwhile lose their copies once it's back on. "Delete exported history" empties the
  copy; while the export is on, every run is then exported again.

### Searchable history

While the export is on, a second setting, `embed_run_history` (on `data_stores`, off by default,
and switched off with the export), embeds the exported messages so agents can search past runs:

- `history.messages` has a full-text vector (`tsv`, generated, GIN index) and an `embedding`
  (HNSW index), from `h0003`. Only what the user and the assistant said is searched or embedded
  (`EMBEDDABLE` in `aios.datastores.models`): not tool results, nor empty text.
- The export embeds what it has just exported once it has caught up (after letting go of the
  export lock, under a lock of its own, so exports and deletes don't wait for it), and a message
  keeps its embedding while its text is unchanged, so a run exported again is embedded only
  where it's new. A message is embedded by its first `EMBED_MAX_CHARS` characters. Switching
  embedding on queues the export, which embeds every message already there (a backfill);
  `data_stores.history_embedded_at` is when it last caught up. An export embeds for at most
  `EMBED_BUDGET`, and the every-minute sweep picks up what's left. Embedding that fails (the
  embedding API is down, say) records why (`history_embed_error`, shown on the Organisation
  page's Settings tab) and is tried again after `EMBED_RETRY_AFTER`, or at the store's next
  export. Switching it off stops new embeddings and keeps the existing ones, which are still
  searched.
- The **Run history search** tool (`history_search`, the agent's `search_history`) runs the
  hybrid search over the store's messages (`aios.search.HISTORY_SQL`): by keyword always, and by
  meaning over what's embedded. If the query can't be embedded, it searches by keyword alone.
  Each result is a message with its run's id, agent, title and when it started; the run
  searching is left out. Like document search, it runs in the store's own database as `_ro`, so
  it never sees another store's runs.

# Stores

Each organisation has a **store**: a Postgres database of its own, on the same instance as the
platform database, which holds its agents, skills and workflows, their runs and the runs'
history, its schedules, its chats with their members, messages and invocations, its agents'
schema change requests, its documents, the data tables its agents asked for, and, when it's
its search index. The platform database holds no organisation content: only identity,
organisations, memberships, invitations, the store registry (with its credentials),
notifications, and the schedule due index. In code and on screen it's a "data store"
(`aios.datastores`, the `data_stores` table, the "Data store" notices); screens show its
contents as **Data**. See also [Accounts and organisations](accounts-and-organisations.md) and
[Tools](tools.md), for the tools that read and write it.

## The Data page

The **Data** page shows the organisation's store, the same for every member: its status
(Overview), its approvals (Approvals: the schema changes its agents asked for and its workflows'
approval steps, filterable by kind and status, those waiting for a decision first, then the
history), the tables and columns they dropped (Dropped: restorable until purged), its data
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
| `aios_provisioner` | `LOGIN CREATEDB CREATEROLE` | creating and migrating stores (`DATA_STORE_PROVISIONER_URL`). On AWS its secret is for the routine worker and the migrate-stores task only. |

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
downloads, approvals and search all resolve the signed-in user's
organisation's store (`aios.datastores.resolve.data_store_for`, the `CurrentDataStore`
dependency), and a run uses its agent's or workflow's organisation's. A user in another
organisation finds none of it (404). Each document records who uploaded it (`uploaded_by`, a
user id: a store has no users table). Signing up makes the new organisation's store's row
(`data_stores`, `owner_organisation_id`, status `provisioning`) and queues
`aios.provision_data_store` on the `routine` queue, which only the routine worker
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
`failed`. The routine worker's beat (`-B`) tries `failed` stores again every ten minutes,
along with any still `provisioning` five minutes after they were made (their task was lost with a
worker, say). Until a store is `ready`, uploads, the document list and search answer 409 ("being
set up"), and the UI says so.

A store whose organisation is deleted (an empty one, on accepting an invitation) goes `deleting`,
with no organisation, in the same transaction. The routine worker's sweep
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
back, until the routine worker's beat brings it to head with the retries above.

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
sweep** (`aios.repair_stores`, on the routine worker's beat every ten minutes), which brings
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
were, owner and visibility included (both dropped by store revision `0019`, when every chat
became open; it also gave chat messages their Jev verdict). Store revision `0022` gives chats
when and by whom an admin archived them (`archived_at`, `archived_by`). Beside the runs they name, they do so by foreign key: a
run deleted clears a schedule's last run, an invocation's run and a request's run; a schedule
deleted clears its runs' `trigger_id`; a waiting step names its request.

Every run then got an invocation, and its turns became records (store revision `0013`). A run
started by hand has a `manual` invocation, a schedule's a `schedule` one naming its trigger, and
a workflow agent node's is a child of its workflow run's (`parent_id`, with the chain of its
ancestors' ids and its depth), taking its cause (`aios.invocations.for_run`). Each turn of an
agent run is a row of `aios.turns`: its run, invocation, agent version, status and error, times,
and model calls, tool calls and tokens (`aios.turns`). A run's status, times and counters are
derived from its turns (its latest turn's status and times, the counters summed), so the run's
own columns for them were dropped. The run page lists the turns, each with any tools it left out
and why (`turns.notices`, store revision `0035`: a connector's tools that were unavailable, a
pinned tool that vanished upstream; see [Tools](tools.md#connector-tools)).

The `reporting` schema holds read-only views for agents to query with `query_data`:
`reporting.runs` (agent and workflow runs: kind, ids, cause, status, times and summed counters)
and `reporting.turns` (each turn's ids, cause, status, times, duration and counters). They hold
identifiers, counts, times and statuses only, never text: no messages, titles, inputs, outputs
or errors. Both roles read them through the store's default privileges; neither may write
through them (`upgrade_store` revokes it each migration).

The `insights` schema (store revision 0030) holds the Insights page's metrics-only views
(`aios.insights`): `insights.turns` (with the workflow a turn ran in and who it's charged to,
and agent runs from before turns were recorded as one untracked turn each, counters null),
`insights.workflow_runs` and `insights.workflow_steps` (times running without the time waited
for approvals), `insights.usage`, `insights.tool_calls` (each turn's calls by tool, from
`aios.turn_tool_calls`), `insights.approvals`, `insights.chats`, `insights.chat_messages`,
`insights.mentions`, `insights.agents` and `insights.workflows`. Identifiers, names, counts,
times, statuses and costs only: no message text, brief, tool input or output, memory, synced
text or error. The panels read them as `_ro` in a read-only transaction, each panel cached per
store, range and grouping for `INSIGHTS_CACHE_SECONDS` (5 minutes). Any migration changing a
table they read updates them in the same migration.

- **Named by store and id.** Ids repeat across stores, so everything outside the store that
  names a row of it carries the store too: worker task arguments (`apply_schema_change_task`),
  the run service's work items, the turn lease's Redis channels, a
  sandbox's session key, the firing of a schedule or an invocation, the schedule due index and
  notifications (named by their organisation, whose store it is, and the row's id: a request's,
  a chat message's, a run's). Trace sessions are
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

**Syncing** (`aios.sync`, store revision `0026`). Every other source syncs through one loop,
whatever its kind, plugged in through an adapter (`aios.source_providers`: changes since a
cursor, a full listing, fetching an item, scope options). So far the only provider is the
development-only test provider (`SOURCES_TEST_PROVIDER_ENABLED`, on in Compose), fed from
in-process feeds (`aios.source_providers.test`). What a source brings in are **synced items**
(`aios.synced_items`, one per external id per source):

- a **message item** keeps its text (cut at `SYNC_MESSAGE_TEXT_CAP_BYTES`, marked as cut), is
  checked by Jev, and is indexed straight into search as `source_item` pieces;
- a **file item** becomes a document (`documents.synced_item_id`), stored in S3 as uploads are
  and ingested by the same pipeline; deleting it on the Documents tab is refused (409): it's
  removed at its source.

A sync writes items idempotently by external id, `SYNC_BATCH_ITEMS` per transaction, fetches and
indexes again only items whose content hash changed, and moves the source's cursor only when it
commits, so one failing part way is resumed by the next without duplicates or re-embedding. A
full sync (a source's first, daily reconciliation, or Full resync) removes what the listing no
longer has, with its document, chunks and search entries. At the storage cap new items are
skipped (counted, "storage full") while updates and removals still apply. Each sync is recorded
(`aios.source_syncs`, pruned after `SYNC_HISTORY_DAYS`); a partial unique index keeps one
running per source. Syncs run on the run service's `sources` lane: each process enqueues the
sources due every `SYNC_POLL_SECONDS`, and Sync now (admins) enqueues one at once, or answers
409 while one runs.

**Health** is derived from the source's status and recent syncs: healthy, delayed (its last sync
failed, or none succeeded for two intervals), failing (`SYNC_FAILURES_BEFORE_FAILING` failures in
a row; retried only every `SYNC_FAILING_RETRY_SECONDS`) or needs reconnecting (a final
authorisation failure). Becoming failing or needing reconnecting tells every admin once
(`source_attention`).

**Jev.** Message items are classified as they're written and whenever their content changes;
file items are classified as documents by ingest, their verdict copied onto the item. Flagged
items are stored but never found by search; they're on the flagged list, where admins release
them (audited). Items Jev can't check stay `awaiting_check`, unsearchable, and the next sync
checks them again (the sync record counts those still waiting). A released item that changes
goes back to `awaiting_check`, and admins are told (`released_item_changed`). A synced document's
document ingested event waits until it's found clean; releasing fires nothing.

**Connectors** (`aios.connectors`, `aios.api.connectors`, store revision `0029`, platform
revision `0078`). A connector is a connection to Slack, Google Drive, GitHub or (in development)
the test provider: one per provider per organisation (`aios.connectors`, unique by provider).
The providers list says which this deployment offers and why any aren't: a provider not in
`CONNECTOR_ENABLED_PROVIDERS` is switched off (taking one out stops its connectors' syncs at
once, and leaves their content searchable), and one without its app settings is unavailable.

- **Connecting** (admins only) is an OAuth-style flow: the state is signed, bound to the admin's
  session, the organisation and the provider, and expires after `CONNECTOR_STATE_TTL_SECONDS`.
  The provider sends the browser back to one route outside the organisation's path,
  `/api/connectors/callback`, which refuses a bad or expired state (400) and one from another
  session or person (403), and lands on the connector's page. A second connector to the same
  provider is refused (409).
- **Credentials** live in the platform database (`connector_credentials`), encrypted with a key
  derived from `CONNECTOR_CREDENTIALS_KEY` (apart from the stores' passwords' key; required in a
  production-like configuration), never in the store; only `aios.connectors.credential` reads
  them, and the API never returns them. A final authorisation failure marks the connector as
  needing reconnection and stops its syncs; Reconnect runs the flow again, and syncing carries
  on from the stored cursor.
- **Choosing what syncs.** Admins pick channels, folders or repositories from the provider's
  searchable options; something restricted at its source needs accepting that everyone in the
  organisation will be able to search it (422 otherwise). The first scope makes the connector's
  source (kind `connector`, `sources.connector_id`), which syncs; a changed scope makes its next
  sync a full one, which removes what left the scope.
- **Calls** go through `aios.outbound`, which reaches only the provider's
  `CONNECTOR_ALLOWED_HOSTS`; a call anywhere else is refused before it's sent, and the sync fails
  with that recorded on it.
- **Removing** a connector stops it at once, deletes its credentials (revoked at the provider
  where it can), and then deletes its content or keeps it: file items' documents move to Uploads,
  and message items become Markdown documents in Uploads, grouped as the provider says
  (`Provider.keep_groups`: the test provider's are one per channel per month, as Slack's will be;
  GitHub's will be one per issue or pull request with its comments). A
  removal that stopped part way is finished by the run service's sync sweep. A connector's source
  is removed only with its connector (409).

Connecting, scope changes and removal (with the keep-or-delete choice) are in the audit log.
Agents act in connected systems with [connector tools](tools.md#connector-tools).

**URL, webhook and email sources** (`aios.url_sources`, `aios.push_sources`, store revision
`0036`, platform revision `0082`). Admins add them on the Sources tab (`GET .../source-kinds` says
which this deployment offers, and why any isn't).

- **A URL source** fetches one web page on its interval as a file item (a document), through
  `aios.url_fetch`: HTTP or HTTPS on ports 80 and 443 only, every address the host resolves to
  must be public (no private, loopback, link-local, metadata, shared or IPv4-mapped private
  address), checked again after every redirect, and the connection is made to the address that
  was checked, so a DNS answer that changes in between is never used. At most
  `URL_FETCH_MAX_REDIRECTS` (5) redirects, `URL_FETCH_MAX_BYTES` (5 MiB) and
  `URL_FETCH_TIMEOUT_SECONDS` (20). Saving an address that fails the check is a 422, "This
  address isn't reachable from aios"; one that turns unreachable later fails its syncs with that.
  Fetches are conditional (the ETag, Last-Modified and content hash are the source's cursor): an
  unchanged page isn't ingested again, a changed one replaces its document's content, and "not
  found" or "gone" removes the document while the source stays active.
- **A webhook source** is a webhook that keeps its deliveries: the same endpoint, signature and
  checks as webhook triggers (see [Triggers](triggers.md)), with its secret encrypted on the
  source and shown once (rotating it changes the URL too). A delivery Jev finds clean is stored
  as a searchable message item, named by its delivery id, and fires webhook received for the
  triggers listening to the source (`filter.sources`); a flagged one is stored flagged (not
  searchable), fires nothing and answers 422; one Jev can't check answers 503 and isn't stored.
- **An email source** has an unguessable address on `INBOUND_MAIL_DOMAIN`, shown once (rotating
  it cuts the old one off). The receiving mail service posts each message to
  `/api/inbound-mail` with `INBOUND_MAIL_SECRET` as its bearer token. A message is kept only from
  an allowed sender (none at first: nothing is kept) that passed SPF or DKIM; anything else is
  dropped and counted on a sync record. Kept, it's a message item threaded under the first
  message of its thread (by `In-Reply-To` and `References`), with its attachments (each within
  `EMAIL_ATTACHMENT_MAX_BYTES`) as file items. Without an inbound mail domain, email sources are
  unavailable. Items Jev can't check yet are checked again by the awaiting-check sweep.

Webhook and email sources never sync, so their items are never reconciled away; what arrives is
recorded as a sync of its own. Only the hash of a webhook source's token, or an email source's
address, is kept (`sources.token_hash`, with the encrypted `sources.secret` on the deny list), and
in the platform's `webhook_tokens` and `inbound_addresses` indexes.

### The deny list

A few columns are left out of the read-only role's grants (`provision.DENIED_COLUMNS`), and so
out of `query_data`, `describe_data` and the Tables view: people's emails in the people mirror,
a run's sandbox's bearer token (`sandbox_sessions.secret`), the audit log's details
(`audit_log.details`), webhook triggers' and webhook and email sources' secrets and token hashes
(`triggers.webhook_secret`, `triggers.webhook_token_hash`, `sources.secret`,
`sources.token_hash`), for now (later: other secrets and credentials). The role keeps the table's other
columns. It's a few grant lines, applied every time a store is migrated (the default
privileges would otherwise give a new table's every column).

`write_data` writes as **the agent's own login role**, `ds_<id>_agent_<agent id>`
(`aios.datastores.agent_roles`; see Table ownership below). Stores made before it had one
writer role, `ds_<id>_writer`, which `_rw` switched to; the store migration command drops it.

### The audit log

Each store has an audit log (`aios.audit_log`, store revision `0014`; module `aios.audit`): who
did what (a dotted action such as `agent.archived`, `approval.decided`, `member.role_changed`
or `data.written`), to what (a target's kind and id), when, and details. The actor is a
person, an agent in a run, or the platform itself. It's append-only: a trigger refuses updates,
deletes and truncation whoever asks, the store's owner included, and `_rw` may only insert and
read it (`provision.APPEND_ONLY`). Its details are on the deny list.

- **Store-side actions** record their entry in the store transaction that acts: configuration
  changes, archiving and unarchiving (agents, skills, workflows, schedules, sources), version
  restores, exports and imports (`agent.exported`, `workflow.exported`, and an `*.imported`
  entry, with the file's name, for each skill, agent and workflow an import makes; a dry run
  records nothing), approval decisions, approval rule changes, restores asked for, chats renamed
  (`chat.renamed`, with the old and new names), archived and unarchived (`chat.archived`,
  `chat.unarchived`), and each `write_data` call (its table, operation, row count, run and agent).
- **Platform-side actions** (role changes, joining and leaving, invitation links, renames,
  settings, deletion scheduled or cancelled) write an outbox row on the platform
  (`audit_outbox`) in their own transaction. The routine worker's beat drains it into each
  store every `AUDIT_DRAIN_SECONDS` (10), idempotently by outbox id (`audit_log.outbox_id` is
  unique), so a store that's briefly unavailable gets its entries once it's back, each once.

Admins read it on the Organisation page's **Audit log** tab (`GET /api/o/{org}/audit-log`,
filtered by actor, action or its family, and days; newest first; cursor-paged). Agents read the
**reporting view** `reporting.audit_log` (who, what, to what and when, without details): the
`reporting` schema holds read-only views for agents, which `describe_data` lists.

### Approval rules

Who may decide each kind of schema change is a store table (`aios.approval_rules`; module
`aios.approval_rules`): creating tables, adding columns and creating indexes need an editor by
default; drops, restores, purges and ownership changes an admin. Admins may raise a kind's role
on the Organisation page's Settings tab (`PUT /api/o/{org}/approval-rules/{kind}`), never lower it
below its default (422); each change is audited. A request needs the highest role among its
operations: one adding a column and dropping another needs an admin. Only members with that
role are notified of it, and only they may approve or reject it (403 otherwise); the Data page's
Approvals tab shows every member the role each request needs, and its decision buttons only to
those who may decide it.

Schema changes are one kind of **approval** (`aios.approvals`), a workflow's approval step the
other (see [Workflows](workflows.md#approval-nodes)). Both are listed, read and decided through
one API, which replaced the schema change routes: `GET /api/o/{org}/approvals` (`?kind=` and
`?status=` filter it), `GET .../approvals/{id}`, `GET .../approvals/{id}/impact` (a schema
change's only; 422 for another kind), and `POST .../approvals/{id}/approve` and `/reject`, each
with an optional note (and a drop's typed confirmation); 403 for a role that may not decide it,
409 once it's decided. One module owns what both kinds share: notifying only those who may
decide one, expiring it (`expires_at`, from the organisation's approval expiry setting, 7 days
by default, `PUT /api/o/{org}/organisation/approval-expiry`; an approval node may set its own),
cancelling it when nothing waits on it any more, clearing its notifications, and resuming the
run waiting on it. An expired approval counts as not approved. Every decision is audited
(`approval.decided`). Approvals are still kept in `aios.schema_change_requests`, with a `kind`,
until the rename to approvals.

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

### Table ownership

**Each data table can have an owning agent** (`aios.table_owners`, `aios.table_shares`; module
`aios.datastores.ownership`). Applying an approved schema change that creates a table records
the agent behind the request's run as its owner, in the same transaction. Only the owner, and
the agents it's shared with, may write an owned table with `write_data`; a table with no owner
(made before owners, by hand, or cleared) is writable by any agent with the tool, and a table
whose owner is archived is read-only for every agent, its shares included, until it's
transferred. A refused write names the owner. Reads are unchanged: every agent reads every table
through `query_data`.

The Tables list shows each table's owner ("No owner", or read-only while its owner is archived)
and shares, and editors and admins change them there: `PUT .../tables/{table}/owner` (assign,
transfer, or `null` to clear), `POST .../tables/{table}/shares` and
`DELETE .../tables/{table}/shares/{agent}`, each answering with the table's access and a
`pending` flag (true until the database's grants carry the change), and each audited
(`table.owner_assigned`, `table.transferred`, `table.owner_cleared`, `table.shared`,
`table.unshared`). Assigning an owner to a table with none only tightens access, so any editor
may; transferring or clearing an owner is an ownership change, which needs the role of the
`change_ownership` approval rule (an admin by default; 403 otherwise). Sharing with an archived
agent, with the owner, twice, or sharing a table with no owner is refused (409), as is making an
archived agent the owner; a new owner loses any share it had. An agent's archive dialog lists the
tables it owns, which archiving leaves read-only. Ownership rows name a table as it's named now:
a drop renames them with the table, a restore renames them back, and a purge deletes them, its
shares with them. `make seed` makes owned tables.

**The database enforces it** (`aios.datastores.agent_roles`). Each agent that has the
`write_data` tool gets its own login role in its store, `ds_<id>_agent_<agent id>`, with a
generated password kept encrypted in the platform's `agent_store_roles` and given to Postgres
only as a SCRAM verifier. `write_data` writes as it (through the pooler, with its own
`CONNECTION LIMIT`, `DATA_STORE_AGENT_CONNECTION_LIMIT`, default 4, and a pool of one connection
plus one while busy per process), and only for writes: nothing else connects as it. Each login:

- is a member of the store's agents' group, `ds_<id>_agents` (no login), inheriting it but
  unable to `SET ROLE` to it (Postgres 16's membership options), and of nothing else; it may
  connect to its own store only, with the store's statement and idle timeouts;
- writes exactly the `data` tables it owns or is shared (column by column where columns are
  dropped), while the group reads every table in the store but the deny list (conversation
  content and memory included), writes the tables nobody owns, and inserts into an explicit list
  of platform tables, `provision.AGENT_WRITES` (the audit log and the event outbox: what a write
  records in its transaction). It never writes conversation content or memory.

`query_data`, the only tool running SQL a model wrote, stays on the store's read-only role. (`_rw`
still writes `data` tables, though nothing of an agent's runs as it; it loses that with the old
forms, phase 3 ticket 09.) Grants are derived from the ownership records in one
place (`dropped.grant_access`): schema changes, drops, restores and purges set them in their own
transactions, and the urgent lane's `aios.apply_agent_access` task (`agent_roles.apply`) makes
logins and re-derives every grant after a share, unshare or transfer, an agent archived,
unarchived or given (or losing) the tool, and an applied schema change. A table's access shows as
`pending` (`aios.table_owners.access_pending_since`) until then; a write the records allow but
the database doesn't yet, or by an agent whose login isn't made yet, answers "being set up, try
again" without failing the turn, and queues the task. Archiving an agent takes away its login and
grants and ends its connections; unarchiving gives back the same login. The store migration
command applies every store's and then verifies them against the records
(`agent_roles.verify`), failing the store on drift. Any role may change its own password and
settings in Postgres; applying sets the platform's verifier and settings again.

The escape suite (`tests/test_agent_logins.py`, the `test_escape_*` tests) tries every known way
out of an agent's role, as the login and through `query_data`; it runs in the backend shards, so
`backend-gate` requires it.

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
routine worker's beat measures every ready store every five minutes, as its `_ro` role,
and uploads and deletes record the documents' bytes straight away. Deleting documents or rows
makes room at once; the Overview shows rows' room at the next measurement. Each measurement also
keeps a sample on the platform (`store_usage_samples`), which Insights draws against the caps
over time; samples older than `STORAGE_HISTORY_DAYS` (400) are pruned nightly with retention. The checks first read
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
and `write_data` while its data stays. The card and the Approvals list show what a drop
would remove (row counts, the columns affected), and approving it takes the tables' names
typed (refused by the server otherwise). The Data page's Dropped section lists what's
dropped with when it's purged, and restores it (on the urgent worker,
`aios.restore_dropped`), refused if its name has been taken since. The routine worker's
beat purges what's older than `DATA_STORE_DROPPED_RETENTION_DAYS` (30) every hour
(`aios.purge_dropped`). Restores and purges are logged in `aios.schema_history` too.

### Retention

**Runs and chat history are kept indefinitely unless an admin says otherwise** (`aios.retention`).
Two settings on the organisation, in the platform database (`organisations.run_retention_days`
and `.chat_retention_days`, platform revision `0067`), from 1 to 36,500 days; null, the default,
keeps everything. Admins set them on the Organisation page's Settings tab
(`PUT /api/o/{org}/organisation/retention`, audited as `settings.changed`); everyone else sees
them read-only. The routine worker's beat prunes every ready store with a setting nightly
at `RETENTION_PRUNE_HOUR` (UTC, 3 by default; `aios.prune_retention`), 1,000 rows per
transaction:

- **Runs** finished longer ago than the setting go: an agent run with its turns, sandbox session
  (stopped too), checkpoint thread and search items; a workflow run with its steps and its agent
  nodes' runs. Never pruned: a run with a running turn, a held lease, an unfinished invocation or
  a request it still waits on, or a turn since the cutoff; a workflow run queued, running or
  waiting; an agent node's run on its own.
- **Turns** of a conversation run (`chat_id` set) used again lately go turn by turn, oldest
  first, older than the run setting, while the run is idle: each turn with its messages in the
  checkpointed history (only the latest checkpoint is kept) and its search items; the latest turn
  always stays. The cut is made only before a person's message, so a tool call is never parted
  from its result. A compaction summary goes once every turn it covers has.
- **Messages** older than the setting go, with their mentions, the invocations they started
  (and those invocations' cards, results, briefs and search items) and their search items. Never
  pruned: a message whose invocation, or one it started, is unfinished or finished after the
  cutoff. Chats stay.
- Documents, data tables, memory, usage and the audit log are never pruned.

Each run of the job writes one audit entry per kind with a setting (`runs.pruned`,
`turns.pruned`, `messages.pruned`, by the platform) with the `count` and the `cutoff`.

### Compaction

**Long conversations keep working** (`aios.compaction`). Before each model call of a run, a
middleware on every agent (`CompactionMiddleware`) puts the run's latest summary
(`aios.run_summaries`, store revision `0037`) in place of the turns it covers, wrapped as
untrusted with the source `derived:compaction` (verdict `derived`). If the model's input still
passes `COMPACTION_THRESHOLD_TOKENS` (120,000, estimated), the oldest whole turns beyond the latest
`COMPACTION_TURNS_KEPT` (6) are folded, with the previous summary, into a new one of at most about
`COMPACTION_SUMMARY_TOKENS` (4,000) by the handoff model. A turn, as the model sees it, begins at
a person's message, so the cut never parts a tool call from its result. Each summary records the
last turn it covers and the first message it doesn't; turns record where their messages end in
the history (`turns.message_end`). Each compaction is a usage ledger entry with the purpose
`compaction`, traced as a child of the turn's trace and tagged `compaction`. The checkpointed
history, the run page (which marks where earlier turns were summarised) and search keep every
turn.

## The search index

`aios.search_items` (store revision `0015`, `aios.search_index`) holds one item per thing worth
finding: a document's chunk, a chat message, what the user or the assistant said in a run's turn
(not tool results), and a handoff's brief. Each writer indexes in the same transaction as its own
row: ingest its chunks, posting a message (or a run posting its result) the message, `_end_turn`
the turn's new messages (every turn ends there: chats, schedules, workflow agent nodes), and the
handoff its brief. `make seed` posts its chat's first message through the same writer. Each kind's
source is a foreign key that cascades, so an item goes with its document, message, run or chat.
Items carry their chat, agent, run and author for filters, and no visibility: everyone in the
organisation can find everything.

Embedding happens in the background: an item is written with no embedding, and found by keyword
until the store's sweep embeds it (an embedding sweep on the [run service](run-service.md),
background work enqueued for every ready store every `SEARCH_EMBED_INTERVAL_SECONDS`), for at
most `SEARCH_EMBED_BUDGET_SECONDS` per store per sweep. An item is embedded by its first
`EMBED_MAX_CHARS` characters, and never while a store session is held. Its `text_hash` keeps
unchanged text from being embedded again: an item written again with the same text keeps its
embedding, and one whose text is already embedded elsewhere in the store takes that embedding.
Ingest no longer embeds a document's chunks (`chunks.embedding` is nullable and left empty).

### The run history export, gone

The run history export (a copy of the organisation's runs in the store's `history` schema, with
its outbox, tasks, settings and panel) is gone: past runs are searched through the index. A store
that had it is still on the store tree's `history` line (`data_stores.history_revision`), whose
head, `h0005`, drops the `history` schema; the line's revisions stay, so a store at any of them is
brought there as it's migrated. No store gets the line any more. Platform revision `0065` drops
the outbox and the export's columns on `data_stores`.

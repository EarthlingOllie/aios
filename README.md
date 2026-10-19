# aios

Chat with your documents. Upload files, they're parsed and chunked with Docling, embedded with
Voyage, and stored in Postgres/pgvector. Each new chat has a **documents assistant**: a LangGraph
agent (LangChain's `create_agent`, on Anthropic) that calls a hybrid search tool (vector +
full-text, fused with Reciprocal Rank Fusion). `/` opens the Chats section (see Chats below).

You can also define your own **agents** (a base prompt plus a set of **skills**, which are
reusable instructions, and **tools** from a built-in library: web search; document search,
the same hybrid search as the documents assistant's, over the documents of the agent's organisation;
and run history search, the same over its exported past runs)
and chat with them. The **Search** page runs that hybrid search directly, with no agent in
between, and shows each passage with how the semantic and keyword branches ranked it. The
**Data** page shows the organisation's data store, the same for every member: its status (Overview), the schema changes its agents
asked for (Changes: those waiting for a decision, then the history), the tables and columns
they dropped (Dropped: restorable until purged), and its documents, with each one's ingest
status, chunk count, size, upload time and who uploaded it, upload, download and delete (the chat's documents
panel in the Chats section uploads to the same list). Each section is a tab with its own URL (`/data?tab=documents`). Each
conversation with an agent is saved as a **run** (a LangGraph thread), whose page shows the
conversation and an event log: messages, model responses, tool calls and results, the
instructions sent and token usage. A run also records how it went and what it cost: a status
(running, succeeded, failed), when its latest turn began and ended, and, over all its turns, the
model's tool calls and its input and output tokens (each model call's, summed, so the context is
counted again at each step, as bills count it). The run list and the run page show these; runs
from before they were kept show "not tracked" rather than zero, and the run page's event log
still totals the stored messages for them.

Agents and skills are **versioned**. Each has an editable identity row (name, description) and a
chain of immutable versions (`agent_versions`, `skill_versions`) holding everything that affects
the model's behaviour:

- Changing an agent's prompt, skills or tools (or a tool's settings) creates a new version.
  Renaming doesn't, and nor does saving without changes (configs are compared by hash).
- Changing a skill's description or content creates a new version, and so does renaming it: the
  name is sent to the model in each agent's instructions, so each skill version snapshots it.
- Each agent version pins specific skill versions and stores the exact rendered instructions.
  Editing a skill doesn't change any agent; the agent shows "Update to vN" and taking the update
  creates a new agent version.
- A run is pinned to the agent version it started on, so editing an agent mid-conversation
  doesn't change it.
- Restoring an old version creates a new one with that config; numbers only go up.
- Skills are soft-deleted (old agent versions still reference their versions), and can't be
  deleted while an agent's current version uses them.
- Each run is traced to Langfuse when it's configured (see [Tracing](#tracing)), with
  `agent_id`, `agent_version`, `agent_version_id`, the skill versions and the tools with their
  settings as trace metadata, and the run's thread id, `agent-run-<id>`, as the Langfuse
  session.

**Workflows** graph agents together, and can run other workflows as subgraphs (`/api/workflows`).
The Workflows page draws each one as a graph (React Flow, laid out with dagre); clicking a node or
an edge shows its configuration. Its editor adds nodes for your agents and workflows, connects
them by dragging between nodes, and removes, renames or re-points them, refusing edges that would
make a cycle before you save. Runs are started from the workflow's page; a run's page draws the
path it took on the graph (which nodes succeeded, failed or never ran, and the edges their
outputs went along) and, while it's going, polls to show where it is now. Clicking a node shows
its step's input, output and conversation, and a subworkflow node opens its own graph. They're versioned like agents (`workflows`,
`workflow_versions`):

- A version is a directed acyclic graph: nodes (`workflow_nodes`), each pinning one agent version
  or one workflow version by id, and edges (`workflow_edges`) between node keys. Nodes with no
  incoming edge are where the workflow starts.
- Changing the graph (nodes, edges, or which version a node pins) creates a new version; renaming,
  moving nodes in the editor (the workflow's `layout`, positions by node key) and no-op saves
  don't. Editing an agent doesn't change any workflow: the node reports its
  latest version, and taking it is a new workflow version. Restore works as for agents.
- A workflow can't contain itself, directly or through other workflows at any depth: none of the
  versions a new version pins, nor any they pin in turn, may belong to it. Nodes can only pin
  versions that already exist, so nesting is always finite.
- Agents and workflows are soft-deleted too: older workflow versions keep pinning their versions,
  and stay readable (a node whose agent or workflow is gone shows no latest version). A workflow
  can't be deleted while a live workflow's current version runs it; older versions don't count,
  but restoring one that runs something since deleted is refused. Deleting an agent still
  deletes its runs, their threads and sandboxes. Names are unique among the organisation's
  live rows, so they can be reused.
- An agent is deleted only once what depends on it is resolved (`aios.dependents`).
  `GET /api/agents/{id}/dependents` lists its chat runs that wait on a schema change request or
  have a turn underway, the workflow runs still going (queued, running or waiting) whose pinned
  version runs it at any depth, the live workflows whose current version runs it (directly, or
  through a nested workflow's pinned version) with their schedules, and its own schedules.
  `DELETE /api/agents/{id}` answers 409 with that list (`detail.dependents`, and a
  `detail.message`) until its body resolves each: `cancel_agent_runs` and
  `cancel_workflow_runs` name the runs to stop, and `deactivate_workflows` the workflows to
  deactivate; any other workflow must first be edited to stop running the agent (the agent's
  page links to its editor). Runs are named one by one, so a workflow run that starts, or a chat
  that starts waiting, after the user looked refuses the delete rather than being cancelled
  unseen. Finished runs and older versions don't count, and a workflow run whose worker died is
  failed first, as reading it would. Cancelling a workflow run fails it
  (`aios.workflows.cancel_runs`), closes out its requests (pending ones cancelled, their
  notifications cleared) and starts no more of its nodes; a worker running it stops at its next
  beat. A chat run is deleted with the agent, as before, cancelling its requests. Any turn still
  underway on the agent's runs (a chat's, a schedule's, a cancelled workflow run's node) is
  stopped: the connection holding its turn lock is ended (`checkpoints.stop_turns`), so it fails
  at its next write, and a model or tool call already underway finishes unrecorded. A turn that
  only takes its run's lock after the run has gone finds it gone and does nothing, so no thread
  is left behind. **Deactivating** a workflow turns off its schedules (as
  pausing each would, dropping a firing claimed but not started), and with the agent gone no
  run of it can start, by hand or by schedule: starting one is refused as running a deleted
  agent. It stays, to be edited (removing the node, or taking a nested workflow's newer
  version) or deleted; its schedules can be turned back on once it runs. It all happens in one
  transaction, with the agent and the workflows that run it locked, so a run being started is
  either seen (and refuses the delete) or waits and then can't start. On the agent's page,
  Delete opens a dialog that loads the list: with nothing in it, a simple confirm; otherwise
  each run (with the node it waits at and its requests), workflow and schedule links to where
  it's seen or managed, in a new tab, with a "Cancel these runs" box and Deactivate or Edit
  workflow for each workflow. Delete stays disabled until each has a resolution, and the list
  is checked again when the window regains focus (or on "Check again"), so a workflow edited
  meanwhile leaves it.
- Saves and deletes lock the rows involved (skills, then agents, then workflows, each by id), so a
  delete can't race a save that starts using what it deletes.
- A **run** (`workflow_runs`) is given one text input and pins the version that's current when it
  starts. A worker of its own runs it (`aios.run_workflow`, a Celery task on the `workflows`
  queue, so ingests don't wait behind runs, with its own time limit, `WORKFLOW_RUN_TIME_LIMIT`),
  refreshing its heartbeat (`heartbeat_at`) as it goes; a run whose heartbeat has stopped (its
  worker died) is failed when it's next read or its task comes round again, and one whose
  worker's event loop has made no progress for `WORKFLOW_RUN_NO_PROGRESS_LIMIT` (5 minutes; stuck
  in a blocking call) is failed by the worker itself, while a node awaiting a slow model or tool
  call isn't. A node starts once
  all its predecessors have succeeded, so independent branches run at the same time; start nodes
  get the run's input, other nodes their predecessors' outputs in edge order, joined with a blank
  line. An agent node is a new agent run of its pinned version, given its input as the user's
  message, and outputs the final reply, so its conversation opens on the run page like any other.
  A workflow node runs its pinned version's graph with its input, and outputs its sink nodes'
  outputs (in node order); so does the run. Each node records a step (`workflow_run_steps`, with
  the path of node keys down to it) when it starts and finishes, committed as it goes, so the run
  can be watched while it's going. Once a node fails nothing else starts, and the run fails with
  the first error. Each agent node is traced like an agent run, with the workflow, its version,
  the run and the node's path added to the metadata.
- Starting a run is refused if any agent version in the graph, at any depth, couldn't run (a tool
  that has left the library, or an agent that has since been deleted), or if it has no nodes.
  Deleting a run deletes its agent runs; deleting the workflow leaves its runs in the database.

**Triggers** start agents and workflows without a user in the loop. Each has a kind, and the
first is a **schedule**: a cron expression (5 fields, or `@hourly`, `@daily`, `@weekly`,
`@monthly`, `@yearly`) in an IANA timezone. The **Scheduler** page lists every schedule, the
next to run first, and creates, edits, pauses and deletes them; each agent's and workflow's page
does the same for its own (`/api/triggers`).

Every user is in an **organisation** (`aios.organisations`), the group that will own what its
members make. Signing in for the first time makes a **personal** one named after the user's
login (suffixed, `sam-2`, when that name is taken), with them as its creator and only member;
the upgrade did the same for every existing user and stamped everything they own (skills,
agents, workflows, schedules, chats) with their organisation and with them as `created_by`
(revisions 0029 and 0030). A user is in exactly one organisation at a time: memberships are a
table, held to one per user by a unique index, and the request's organisation is resolved in
one place (`aios.auth.current_organisation`, the `CurrentOrganisation` dependency), never named
in a URL or header. `GET /api/me` says which organisation the user is in and the shell shows
its name, which opens the **Organisation** page (`GET /api/organisation`): the name, the creator
and the members with when each joined. Only the creator renames it (`PUT /api/organisation`; a
403 for another member, a 409 for a name another organisation has): a stopgap role until roles
exist. The **Settings** page is the user's own and links to it.

**Agents, skills, workflows and schedules** belong to the organisation: every member lists,
reads, edits, versions, restores, runs and deletes every one, whoever made it, and to a user in
another organisation each is a 404 (each API module's `get_org_*` checks `organisation_id`
against `CurrentOrganisation`, and lists filter by it). Names are unique within the organisation
(a 409 names the clash), so two organisations can each have a "Reviewer". Each says which member
made it and which last changed it (`created_by`, `updated_by`, reported as the user, shown on
lists and pages: `aios.api.provenance`), each version which member saved it, and a deleted one
records who deleted it (`deleted_by`). A workflow or schedule can only run the organisation's
agents and workflows, and deleting an agent finds what depends on it across the organisation.
Saves still write `owner_id` (the creator) for the release before, until the contract step drops
it. The **data store** is the organisation's (see [Data stores](#data-stores)), and so is the
run history export, on the Organisation page.

The creator brings people in with **invitation links**, made on the Organisation page
(`POST /api/organisation/invites`, `expires_in_days` 1 to 30, a week by default): the response
carries the link's random token once, and only its hash is stored, so the page shows the link to
copy and never again. `GET` lists the open links (who made each, when it expires) and `DELETE
.../invites/{id}` revokes one; all three are the creator's (a 403 for another member). The link
opens the **invite page** (`/invite/<token>`; `GET /api/invites/{token}`, no sign-in needed), which
names the organisation; an expired or revoked link is a 404 and says so. Signed out, Sign in goes
through `/login?invite=<token>`, which keeps the token in the session through GitHub's round
trip; the callback joins a new user to the organisation (the membership records the link, shown
beside the member) and makes no personal one, and sends a user it couldn't join back to the
invite page. Signed in, Accept (`POST /api/invites/{token}/accept`) joins, since a user is in
one organisation at a time, a user who's in none or in an **empty** one: personal, theirs alone,
and holding nothing but the defaults (no agents but the documents assistant, no workflows,
skills, chats, schedules, documents or data tables). The empty one is deleted as they join, and
its data store goes `deleting` for the provisioning worker to drop (see Data stores). Otherwise
it's a 409 with the reason: when their organisation has things in it, how many of each kind, which
the invite page lists with links to where each can be deleted. A member **leaves** from the Organisation page (`POST /api/organisation/leave`): they drop
out of the organisation's chats, their unread notifications about its schema change requests go,
and they land in a new personal organisation with a data store of its own; what they made stays.
The last member can't leave, nor can the creator while others remain (each a 409, explained on
the page).

**Chats** are group conversations, and belong to the organisation: a chat of another
organisation doesn't exist for you (a 404 on every endpoint), and names are unique within the
organisation (a 409), like channels, so two organisations can each have "general". The
**Chats** page lists the organisation's chats you're in, and its public ones you aren't (marked
"Not joined", so you can find them), most recently active first, and opens one to its header
(which says which member created it: `created_by`, `updated_by`, as `aios.api.provenance`
reports them), messages and members, grouped by kind: users, **agents and workflows**, with the
icons the navigation uses. The owner adds any member of the organisation, and any of its agents
and workflows, whoever made them (`POST /api/chats/{id}/members` with `{kind, id}`; a user from
outside it is a 404, as one who doesn't exist is; one row per chat and user, agent or workflow; an
agent or workflow since deleted is no longer a member). A chat is **public** or **private**: a
private chat doesn't exist for anyone who isn't a member, while a public one can be opened by every
member of the organisation. Whoever creates a chat owns it and is its first member (a role in the
chat, not ownership of data); only the owner renames or describes it, changes its visibility, adds
or removes members and deletes it (a 403 for other members). A public chat you aren't in can be
read but not posted in until you **join** it, in one click
(`POST /api/chats/{id}/join`); any member but the owner can **leave** (`POST
/api/chats/{id}/leave`), and the chat leaves their list (a private one is then a 404 for them).

Each new chat has the organisation's **documents assistant** as its first agent
(`aios.assistant`): an agent of the organisation's, made for whichever member first creates a
chat (`agents.builtin = 'documents'`, one live one per organisation), with the document search
tool and instructions to ground its answers in the
results, citing sources. It's mentioned and answers like any agent member (acknowledgement, brief,
card and reply, below), and, running as its owner, searches the documents of the user who owns
the chat. It's listed with the organisation's other agents, where it can be edited like one;
deleted, a fresh one joins the next chat. Chats made before it existed don't get it, but their owner can add it
like any agent. `/` lands on the most recently active chat you're in, or the new-chat form.
Leaving or being removed deletes nothing: whoever joins or is added later reads the history from
the start.

Each chat you're in shows an **unread count** in the list: the messages others posted since the
last one you saw, tracked per chat and member by message id (`chat_members.last_read_message_id`).
Reading the open chat to the bottom, with its tab visible, reports the newest message (`POST
/api/chats/{id}/read` with `{message_id}`; it only moves forward) and clears the count; the list
asks again every 5 seconds (30 while hidden). Joining counts what was already there as read; a
member added by the owner has the history to read. Nothing is sent off-app.

Members post **messages** in markdown, shown with their author's avatar and name. The open chat
asks for what's new every second (`GET /api/chats/{id}/messages?after=N`, every 30 seconds while
its tab is hidden, and at once when it's shown again), follows new messages when scrolled to the
bottom and otherwise leaves the reader where they are; scrolling to the top loads the page before
(`?before=N`, 50 at a time). Anyone a chat exists for can read it; only its members post (a 403
for anyone else). A read returns `{messages, invocations}` (below). Posting makes a chat the most recently active; a message stays when its
author is deleted, with no author, or removed from the chat.

Typing `@` in the composer offers the chat's members (`GET /api/chats/{id}/mentionable`), grouped
by kind so an agent and a workflow with the same name are told apart. Picking one inserts a token,
`@[name](kind:id)`, and the message is posted with its **mentions** (`{body, mentions: [{kind,
id}]}`), stored structurally as `[{kind, id, name}]`; the message shows each as a chip with the
member's kind and name, and a message that mentions you is highlighted. Mentioning anyone who isn't
a member is a 422 (and a token typed by hand for one is plain text).

Mentioning a **workflow** starts a run of it (`aios.invocations`, `chat_invocations`). The post
creates, in the message's own transaction, a queued invocation per workflow mentioned and the
workflow's acknowledgement with a **run card** under it, and returns them with the message
(`{messages, invocations}`), so the chat never shows a mention without its card. Just after the
response, the run starts through the same helpers starting one by hand and schedules use (and the
firing machinery, `aios.firing`), with the message as its input (each mention as `@name`),
executing as the workflow's owner; the invocation records who asked, the run, and goes `running`.
A start that's refused (no nodes, an agent that can't run, the queue down) fails the invocation
with the reason. When the run ends (in the worker, found stopped, or deleted before it finished),
its output or error is posted back as a message from the workflow, belonging to the invocation,
and the invocation goes `succeeded` or `failed`, in one transaction. One whose start was lost with
its process fails when the chat's next read finds it still queued (or briefing) after 90 seconds. A read's
`invocations` are the state of each invocation its messages belong to and of every one in the
chat still going, each with its run as the run's page shows it (graph, steps), so a chat's live
cards cost its one poll a second. The card draws the run's graph still, at a fixed height, with
each node's state, "n of m nodes done" and a status badge, and opens the run's page when clicked;
a run that couldn't start shows why, and one since deleted says so. Mentions in what a workflow
posts start nothing, so a chat can't loop. Runs started from a chat are listed with the
workflow's others, marked with the chat (while it exists); deleting a chat deletes its messages
and invocations, not the runs.

Mentioning an **agent** runs one turn of a new run of its current version, with the message as
the user's, executing as the agent's owner; its acknowledgement and card come with the post in the
same way. The turn runs in an API process through the firing machinery schedules use, under the
same cap (`SCHEDULER_MAX_CONCURRENT_RUNS`): a process with a free slot claims it, just after the
post or on a scheduler tick (until then it waits, queued, however long), and keeps its heartbeat
while it runs. A turn whose heartbeat stops (its process killed, say, by a deploy) fails its card
with "The service stopped mid-run." when the chat is next read or a tick sweeps, and a turn in
flight as a process shuts down records the same. A process with `SCHEDULER_ENABLED=false`
runs no chat turns either. A read's `invocations` carry each agent run's status, start and finish
times, tool calls and tokens so far; the card shows a status badge, the time elapsed (ticking in
the browser from the start time), the tool calls and tokens, and opens the run's page when
clicked. When the turn ends, the agent's final reply is posted back as a message from the agent
(or the turn's error, failing the card). Several agents and workflows mentioned in one message
each start; mentions in an agent's reply start nothing. Runs started from a chat are listed with
the agent's others, marked with the chat.

Before a mention's run starts, a **handoff** step (`aios.handoff`) briefs it on the chat, so the
member doesn't have to be told the context again. It runs for agents and workflows alike, and is
never skipped: the invocation goes `queued` → `briefing` → `running`, and one structured call to
the handoff model reads the chat up to the mentioning message (its last `HANDOFF_WINDOW_MESSAGES`
messages, 30 by default, or about `HANDOFF_WINDOW_TOKENS`, 8000, of them, whichever is less;
run cards aside) and returns `{ack, brief}`. The ack, a line saying what the member understood
it's being asked to do, replaces the acknowledgement's placeholder ("Reading the chat…"), so
whoever asked can correct it early; a read's invocations carry it (`acknowledgement`), so a card
already on screen updates. The brief is kept on the invocation (`brief`) and the run's input is
the brief, a blank line, then the message (just the message when the chat had nothing to brief it
on). The card shows the brief behind a disclosure, so anyone can see exactly what the run was
told. A handoff that fails (the model errs, takes over a minute, or answers without `{ack,
brief}`) fails the invocation with why, as a refused start does. The model is `HANDOFF_MODEL`,
by default the deployment's `LLM_MODEL`; each call is traced to Langfuse tagged `handoff`, with
the invocation's id and its chat as the session (see [Tracing](#tracing)).

- An agent's schedule carries a message. Each time it fires, a new run of the agent's *current*
  version starts with that message as the user's, and runs one turn exactly as a chat turn does
  (the run is listed with the agent's others, marked as scheduled). The schedule shows how its
  last run went, with a link to it.
- A workflow's schedule carries an input. Each time it fires, a run of the workflow's *current*
  version starts with it, exactly as starting one by hand does (refused the same way, which the
  schedule records as a failure), and is queued for the worker. The schedule shows how that run
  is going, then how it went, with a link to it.
- Each API process runs the scheduler (`aios.scheduler`), polling every
  `SCHEDULER_INTERVAL_SECONDS` (15). Due schedules are claimed with `FOR UPDATE SKIP LOCKED`
  and moved on to their next run in the same transaction, so with any number of replicas each
  run fires once. Set `SCHEDULER_ENABLED=false` to keep a process from running them.
- A process fires at most `SCHEDULER_MAX_CONCURRENT_RUNS` (5) schedules at once (an agent's turn
  runs in the API process, next to the chats it serves), and claims no more than it has free
  slots for. What it can't start stays due, for another replica or its next poll, so a burst
  (many schedules sharing `0 9 * * *`) runs a little late rather than crowding out chat.
- The firing itself is shared machinery (`aios.firing`), for whatever causes a run outside a
  request: a schedule now, and later a mention in a chat. It holds the cap, the heartbeat, the
  stall sweep, and "start a run and run one turn of it"; what's the cause's own (what it claims
  and how it starts the run, where its heartbeat and outcome live) is a small object the caller
  supplies. While a fire runs, the process keeps a heartbeat on the run itself
  (`agent_runs.heartbeat_at`) as well as the cause's, so a run whose process died is failed on
  its own page too, and not only on its schedule.
- Missed runs aren't caught up: a schedule that's overdue (nothing was running) fires once, then
  carries on from now. Across daylight-saving changes it behaves like cron: a skipped local time
  runs when the clock jumps, and a repeated one runs once. A turn still running when the process
  stops keeps what it got to, and its schedule records that it failed. A process that dies
  can't record that, so while a schedule is running the process firing it refreshes a heartbeat
  (every 30s), and any replica's tick fails a running schedule whose heartbeat has stopped for
  90s; replicas that are up keep beating their own, so they're left be.
- Pausing or editing a schedule that has just been claimed, before its run has started, drops
  that firing (it was for the old settings); the trigger row is locked while the run is created,
  so an edit racing it either drops it or waits and lets the started run finish.
- Deleting an agent or a workflow deletes its schedules. Cron parsing is `cronsim`'s, wrapped in
  `aios.cron`.

```
browser ──▶ ALB / nginx ──┬─ /api/*, /login, /logout, /auth/* ──▶ api (FastAPI + Uvicorn)
                          └─ everything else ─────────────────▶ web (React SPA, static)
api ──enqueue──▶ Redis ──┬─▶ worker (Celery: Docling ▶ HybridChunker ▶ Voyage embeddings)
                         └─▶ workflow-worker (Celery, `workflows` queue: workflow runs; api image)
api, worker ──▶ Postgres 17 + pgvector, as `aios_app` (agent run history: LangGraph checkpoints, `langgraph` schema)
api, worker ──▶ S3 (RustFS locally): uploaded files, under each data store's prefix
browser ──download link (presigned, a minute)──▶ S3
api, worker ──traces──▶ Langfuse (optional, self-hosted)
```

| Layer | Stack |
| --- | --- |
| Backend | Python 3.13, uv (exact pins, `exclude-newer = "7 days"`), hatchling, FastAPI + Uvicorn, Celery, pydantic-settings, SQLAlchemy + Alembic, psycopg 3 |
| Auth | Authlib GitHub OAuth; Starlette `SessionMiddleware` (itsdangerous-signed cookie) holds the OAuth state, then the user id |
| Frontend | React + TypeScript, Vite, pnpm (`minimumReleaseAge: 10080` = 7 days), Tailwind v4 + shadcn/ui, Vercel AI SDK `useChat`, react-markdown + remark-gfm; markdown editing via `@github/markdown-toolbar-element` on plain textareas |
| Retrieval | pgvector HNSW (cosine) + Postgres FTS (`tsvector`, GIN), RRF (k=60) |
| Inference | LangGraph agents (LangChain `create_agent`, `anthropic:claude-sonnet-5-5` via `langchain-anthropic`), streamed to the UI by our own Vercel AI SDK adapter (`ui_stream.py`); LangGraph Postgres checkpointer for run history; `langchain-voyageai` embeddings (`voyageai:voyage-4`, 1024-d); Docling `HybridChunker` |
| Tracing | Langfuse (open source, self-hosted) via its LangChain callback handler |
| Infra | Docker Compose locally (RustFS for S3); Terraform on AWS: VPC, ECS Fargate, ALB, RDS Postgres, ElastiCache (Valkey), S3 (SSE-KMS), ECR, Secrets Manager |

## Layout

```
backend/            FastAPI app, Celery worker, Alembic migrations, tests
  src/aios/
    main.py         app + SessionMiddleware
    auth.py         /login, /auth/callback, /logout, current_user, current_organisation
    organisations.py  organisations: a personal one per user on sign-up, the user's one membership,
                    invitation links, joining and leaving
    api/documents.py upload/list/download/delete, GET /api/search
    api/skills.py   CRUD /api/skills
    api/agents.py   CRUD /api/agents, runs: /api/agents/{id}/runs[/{run_id}[/chat]]
    api/workflows.py CRUD /api/workflows, versions and restore, runs: /api/workflows/{id}/runs
    api/triggers.py CRUD /api/triggers (schedules), schedule preview, timezones
    api/chats.py    CRUD /api/chats, members: /api/chats/{id}/members[/{member_id}], mentionable, messages
    api/organisation.py GET /api/me, GET/PUT /api/organisation (the Organisation page), invitation
                    links (/api/organisation/invites, /api/invites/{token}[/accept]), leaving
    api/provenance.py who made and last changed an organisation's things, as the API reports it
    api/settings.py GET/PUT /api/organisation/settings (the organisation's: the run history export)
    scheduler.py    fires due schedules (runs in each API process)
    firing.py       runs an agent's turn for a cause outside a request: the cap, heartbeat, stall sweep
    invocations.py  what a mention in a chat starts: invocations, their cards and results posted back
    cron.py         cron expressions in a timezone (cronsim)
    workflows.py    workflow graph validation, and running workflows
    dependents.py   what depends on an agent (runs, workflows, schedules), checked before deleting it
    runner.py       builds the LangGraph agent for a turn of a user-defined agent's run
    checkpoints.py  run history: LangGraph's Postgres checkpointer, one thread per run
    ui_stream.py    Vercel AI SDK UI message stream <-> LangChain messages / LangGraph streams
    llm.py          the chat model (LangChain "provider:model" names)
    tracing.py      Langfuse callbacks and trace metadata
    versioning.py   creating agent/skill/workflow versions, rendering instructions
    tools/          the tool library agents can be given (registry + one module per tool)
    api/tools.py    GET /api/tools (the library, with each tool's settings schema)
    sandbox/        sandboxes for the run_python tool: sessions, backends, the in-sandbox
                    runner and the egress proxy
    agent.py        the run context (`ChatDeps`) agents' tools read
    assistant.py    the documents assistant: the built-in agent each new chat gets
    datastores/     data stores: data_store_for(user), the one place a user becomes a store,
                    store_session(id, role), the only way to open a session on user data,
                    models.py (the tables every store has: documents, chunks; and the
                    run history export's), history.py (the run history export),
                    provision.py (creating, migrating and deleting each store's database),
                    files.py (uploaded files in S3, and download links), and
                    migrations/ (the store Alembic tree)
    search.py       hybrid search SQL (RRF)
    ingest.py       Docling convert → HybridChunker → embed → store
    worker.py       Celery app + ingest, workflow run and data store provisioning tasks
    db_roles.py     creates the database roles (`aios_app`, `aios_provisioner`) as the master user
  sandbox/          the sandbox image's pinned libraries (requirements.in/.txt)
  Dockerfile        targets: api, worker, sandbox
frontend/           Vite React SPA (+ nginx images/configs)
infra/terraform/    AWS
infra/deploy.sh     build → push → create roles → migrate → migrate data stores → roll out
infra/claude/       Claude Code image for debugging the Compose stack
docker-compose.yml  db, redis, rustfs, files-bucket, db-roles, migrate, migrate-stores, api,
                    worker, workflow-worker, provisioning-worker, web, sandbox
                    (+ claude, docker-proxy with --profile debug)
```

## Prerequisites

1. A **GitHub OAuth app** (Settings → Developer settings → OAuth Apps). Callback URL:
   `<PUBLIC_URL>/auth/callback`, e.g. `http://localhost:8080/auth/callback` for Compose,
   `http://localhost:5173/auth/callback` for `pnpm dev`.
2. An **Anthropic** API key and a **Voyage AI** API key.

## Run everything in Docker

```sh
cp .env.example .env     # fill in GitHub + API keys and a SESSION_SECRET
docker compose up --build
open http://localhost:8080
```

`db-roles` creates the database roles as the master user (see [Database roles](#database-roles)),
then `migrate` runs `alembic upgrade head` as `aios_app` before `api`/`worker` start, and
`migrate-stores` brings the data stores to the store tree's head (see [Data stores](#data-stores)).
`rustfs` stands in for S3 (its S3 API on http://localhost:9000, where download links send the
browser), and `files-bucket` makes the bucket. The worker image bakes in
Docling's models and the chunker tokenizer at build time, so it needs Hugging Face access
while building but not at runtime.

### Debug with Claude Code

The `claude` service (compose profile `debug`) runs [Claude Code](https://code.claude.com) on the
stack's network, so an agent can debug with the same context you have: the repo (bind-mounted
at `/workspace`, so its edits land in your working copy), the database and Redis (`psql`,
`redis-cli`), the api/sandbox over HTTP, and every container's logs (`docker compose logs`).

```sh
docker compose up -d                              # the stack to debug
docker compose --profile debug run --rm claude    # interactive Claude Code session
docker compose --profile debug run --rm claude claude -p "why did the last ingest fail?"
```

- **Auth**: log in with `/login` on first run; it's kept in the `claude-home` volume. If `.env`
  has `ANTHROPIC_API_KEY`, Claude Code offers to use that key (API billing) instead.
- **Docker access is read-only**: it goes through `docker-proxy`, a socket proxy that allows
  logs, ps and inspect but no exec, restart or build. The agent tells you what to restart.
- **Context**: `infra/claude/CLAUDE.md` (installed as Claude Code's managed memory) describes
  the services and the rules above to the agent.
- The backend venv, caches and `frontend/node_modules` are volumes, so they don't clobber your
  host's. Run `uv sync` / `pnpm install` inside the container before running checks there.
- On Linux, files the agent writes are owned by uid 1000. If yours differs, build with
  `CLAUDE_UID=$(id -u) docker compose --profile debug build claude`.
- To upgrade Claude Code, bump `CLAUDE_CODE_VERSION` in `infra/claude/Dockerfile` (keep it
  at least a week old, per the dependency policy).

## Local development

```sh
docker compose up -d db redis rustfs

cd backend
cp .env.example .env     # PUBLIC_URL=http://localhost:5173
uv sync --extra ingest   # drop --extra ingest if you're not running the worker
uv run python -m aios.db_roles   # as the master user (DATABASE_ADMIN_URL); safe to re-run
uv run python -m aios.datastores.files create-bucket   # uploaded files' bucket, in RustFS
uv run alembic upgrade head
uv run python -m aios.datastores.provision migrate-all   # data stores, after every upgrade
uv run uvicorn aios.main:app --reload
uv run celery -A aios.worker worker --loglevel=INFO   # separate shell
uv run celery -A aios.worker worker -Q workflows --loglevel=INFO   # another, for workflow runs
uv run celery -A aios.worker worker -Q provisioning -B --loglevel=INFO   # and data stores (-B: their retries)

cd frontend
pnpm install
pnpm dev                 # http://localhost:5173, proxies /api, /login, /auth to :8000
```

Checks: `uv run ruff check . && uv run ruff format --check . && uv run pytest` (backend; DB tests
need the migrated database: they skip when no server is listening or it isn't migrated, but fail
when the server turns `DATABASE_URL` away (a missing role or database, a wrong password), so run
`aios.db_roles` first. The role tests in `tests/test_db_roles.py` also need `DATABASE_ADMIN_URL`,
from the environment or `.env`, and skip without it, and the documents tests need RustFS, at
`DATA_STORE_FILES_ENDPOINT` with `DATA_STORE_FILES_ACCESS_KEY`/`_SECRET_KEY`, as in
`backend/.env.example`), `terraform test` in `infra/terraform` (it plans and applies against a
mocked AWS, so needs no account) and `pnpm build && pnpm test && pnpm test:e2e`
(frontend). The browser tests (Playwright, `frontend/e2e/`) run the app against an in-memory fake of the API (`e2e/fake-api.ts`), so they
need no backend. Run `pnpm exec playwright install chromium` once first, or point
`PLAYWRIGHT_CHROMIUM_EXECUTABLE` at a Chromium you already have.

### Database roles

Nothing in the app connects to Postgres as the master user (`aios`, RDS's or Compose's):

| Role | Attributes | Used by |
| --- | --- | --- |
| `aios_app` | `LOGIN`; no `CREATEDB` or `CREATEROLE` | the API, the workers and migrations (`DATABASE_URL`). It owns the platform schema (`public` and everything in it), so it can migrate it, and may create schemas (the checkpointer's `langgraph`). It doesn't own the database, so it can't drop or reconfigure it. |
| `aios_provisioner` | `LOGIN CREATEDB CREATEROLE` | creating and migrating data stores (`DATA_STORE_PROVISIONER_URL`). On AWS its secret is for the provisioning worker and the migrate-stores task only. |

`python -m aios.db_roles` creates them as the master user (`DATABASE_ADMIN_URL`, which nothing
else reads), taking each role's name and password from its URL. It also creates the `vector`
extension (not one a non-superuser can create), grants `aios_app` `CREATE` on the platform
database, and makes it own `public` and anything else in the database the master user still owns, so a database that was migrated as the master user is
handed over; the master user stays a member of `aios_app`, for break-glass access. It's safe to
re-run: to change a password, change the URL and run it again.

It changes nothing (it runs in one transaction) and exits with an error if: the server is older
than PostgreSQL 16 (before 16, `CREATEROLE` reaches roles it didn't create); the three URLs name
fewer than three roles, or different hosts or ports (compared as written, so not `localhost` in
one and `127.0.0.1` in another); `aios_app` has, or may `SET ROLE` to a role that has,
`SUPERUSER`, `CREATEDB`, `CREATEROLE`, `BYPASSRLS` or `REPLICATION` (on RDS, don't grant it
`rds_superuser`); or the master user would still own something in the database it can't hand
over, which it lists. Compose
runs it as the `db-roles` service, and `infra/deploy.sh` as the `db-roles` task before every
migration. The master user is otherwise for break-glass access only.

It also revokes `CONNECT` from `PUBLIC` on the platform database (then grants it to the two
roles), `postgres` and `template1`, so no other role (a data store's included) can connect to
them. Only a database's owner may do that, so one the master user doesn't own is left as it is
and reported as a warning. And it makes the data stores' template, `aios_store_template`
(`DATA_STORE_TEMPLATE`): an empty database with pgvector and pgstattuple (for measuring a store's
data) in an `extensions` schema, which no one may connect to and the provisioner copies. It adds
pgstattuple to any store made before the template had it. Stores it may not connect to, or
create it in (on RDS, the master user isn't a superuser), are listed in one warning; any of
them without it is measured by its database's size until a superuser adds it.

### Data stores

Each organisation has a data store: a Postgres database of its own on the same instance (see
`plan/user-databases/plan.md` on the plan branch), which every member reaches: the Data page,
uploads and downloads, approvals and the run history export all resolve the signed-in user's
organisation's store (`aios.datastores.resolve.data_store_for`, the `CurrentDataStore`
dependency), and a run uses its agent's or workflow's organisation's. A user in another
organisation finds none of it (404). Each document records who uploaded it (`uploaded_by`, a
user id: a store has no users table). Signing up makes the new organisation's store's row
(`data_stores`, `owner_organisation_id`, status `provisioning`) and queues `aios.provision_data_store` on the
`provisioning` queue, which only the provisioning worker reads. That task, as
`aios_provisioner`:

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
  the owner creates and `_ro` only read them; neither may create anything;
- runs the store Alembic tree (`aios.datastores.migrations`, version table
  `aios.alembic_version`) as `_owner`, takes writes on the tree's bookkeeping
  (`aios.alembic_version`, `aios.schema_history`) back from `_rw`, and marks the store `ready`
  at its head revision. The tree makes the `aios` schema (the platform's tables) and an empty
  `data` schema (the owner's tables, which the tree otherwise leaves alone).

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
so a sweep cut short is finished by the next.

The login roles' passwords are generated with the row, stored encrypted with a key derived from
`DATA_STORE_CREDENTIALS_KEY`, and sent to Postgres only as SCRAM verifiers, so the app's logs
never have them, and a server logging statements has only the encrypted ones (in the
`data_stores` insert), useless without the key. The API returns only a store's status and
usage (`GET /api/data-store`).

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
and the document search tools') and downloads as `_ro`. Searches have no owner filter: a store
holds only its organisation's documents, and its roles can't connect to any other database. A store is reached at
`DATABASE_URL`'s address (and with its options, like `sslmode`): stores are on the platform
database's instance, which the provisioner may know by another name (Compose's `db`, with the
API on the host). A row's `host` and `port` are for a store moved to another instance later;
they're empty until then.

Each role may hold `DATA_STORE_CONNECTION_LIMIT` connections (default 30), and each process
pools up to 3 per store and role, so the limit must be at least 3 x the processes that use a
role, counted twice for a rolling deploy's old and new tasks (28 at most on AWS: see
`config.py`). A connection past the limit is refused: the API answers 503 to try again, the
ingest task retries, and the search and data tools tell the model.

The Data page's **Tables** (`GET /api/data/tables`, `.../tables/{table}/rows` and
`.../tables/{table}/value`, in `aios.api.data` and `aios.datastores.tables`) show the store's
`data` tables, with their columns and row counts (the planner's estimate for a big table), and
their rows a page at a time. They read as `_ro` through `aios.datastores.reads`, as
`query_data` does, so under the same statement timeout and row and byte caps. No SQL is taken:
a table, a sort column or a value's column is a name looked up in the catalog (404 or 422 if
it isn't there), and only then quoted into a statement. Values come as text, cut to 200
characters (read in full, as far as the byte cap, on asking); embeddings come as their type and
dimension (`vector(1024)`), never their numbers, and `jsonb` pretty-printed.

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

#### Run history export

On the Organisation page any member can switch on `export_run_history` (on `data_stores`): a
one-way copy of the organisation's runs (every member's and every schedule's, matched to the
store by their agent's or workflow's organisation) into its store's `history` schema, keyed by the platform's ids and recording who
ran each. Agent runs go in `history.agent_runs`, with `history.messages` (role, text, tool calls
and usage, flattened from the run's checkpoint). Workflow runs go in `history.workflow_runs`,
with `history.workflow_run_steps`: every step at every depth, with its path, kind, status,
input, output, error and timings, and its agent run (a `history.agent_runs` id, not a foreign
key: the two are exported apart). Statuses are copied as text. The platform database stays the
source of truth (`aios.datastores.history`):

- The `history` tables are the store tree's second line (branch label `history`, revisions
  `h0001`, ...), which a store gets only once the export is first switched on:
  `aios.set_up_history_export`, on the `provisioning` queue, runs it, and from then on every
  migration keeps it at its head (`data_stores.history_revision`). The stores' roles get the
  same default privileges on it as on `aios`. A store behind the line's head isn't exported
  until it's brought there.
- Switching it on backfills: every run in the organisation, agent and workflow runs alike, goes in the
  outbox, `history_exports` (a row per run, by kind, and store). Every agent turn ends in
  `_end_turn` (chats, schedules and workflow agent nodes alike, failed or stopped too), and
  every workflow run in `aios.workflows._end_runs` (finished, failed or stopped), each writing
  the run's row in the same transaction as the run's own update. Deleting a run, its agent or
  its workflow run writes it too, and deleting an agent writes the workflow runs whose steps
  held its runs. Pausing and resuming a workflow run will write it too, once runs can pause.
- `aios.export_history` (default queue: it writes as the store's `_rw`) drains a store's outbox,
  upserting each run with its messages or steps, or deleting the copy of a run that's gone.
  It's queued when the outbox is written, and the provisioning worker's beat queues it every
  minute for any store with rows still pending. A run that fails to export keeps its error on
  its outbox row and is tried again at its next change, or in an hour, so it doesn't hold up
  the rest.
- Switching it off stops the export (after the batch under way) and keeps the copy; runs
  deleted meanwhile lose their copies once it's back on. "Delete exported history" empties the
  copy; while the export is on, every run is then exported again.

##### Searchable history

While the export is on, a second setting, `embed_run_history` (on `data_stores`, off by default, and
switched off with the export), embeds the exported messages so agents can search past runs:

- `history.messages` has a full-text vector (`tsv`, generated, GIN index) and an `embedding`
  (HNSW index), from `h0003`. Only what the user and the assistant said is searched or embedded
  (`EMBEDDABLE` in `aios.datastores.models`): not tool results, nor empty text.
- The export embeds what it has just exported once it has caught up (after letting go of the
  export lock, under a lock of its own, so exports and deletes don't wait for it), and a message
  keeps its embedding while its text is unchanged, so a run exported again is embedded only where
  it's new. A message is embedded by its first `EMBED_MAX_CHARS` characters. Switching embedding
  on queues the export, which embeds every message already there (a backfill);
  `data_stores.history_embedded_at` is when it last caught up. An export embeds for at most
  `EMBED_BUDGET`, and the every-minute sweep picks up what's left. Embedding that fails (the
  embedding API is down, say) records why (`history_embed_error`, shown on the Organisation page) and
  is tried again after `EMBED_RETRY_AFTER`, or at the store's next export. Switching it off stops
  new embeddings and keeps the existing ones, which are still searched.
- The **Run history search** tool (`history_search`, the agent's `search_history`) runs the hybrid
  search over the store's messages (`aios.search.HISTORY_SQL`): by keyword always, and by meaning
  over what's embedded. If the query can't be embedded, it searches by keyword alone. Each result
  is a message with its run's id, agent, title and when it started; the run searching is left
  out. Like document search, it runs in the store's own database as `_ro`, so it never sees
  another store's runs.

### Dependency policy

- **Python**: every dependency in `pyproject.toml` is pinned with `==` and `uv.lock` is
  committed. `[tool.uv] exclude-newer = "7 days"` means `uv lock` only picks releases that are
  at least a week old. Upgrade with `uv lock --upgrade-package <name>` and update the pin.
- **Node**: `pnpm-workspace.yaml` sets `minimumReleaseAge: 10080`; `package.json` uses exact
  versions (`pnpm add --save-exact`).

### Changing models

`LLM_MODEL` takes any LangChain `init_chat_model` string ("provider:model"; other providers need
their `langchain-*` package). The tools and the UI stream adapter expect Anthropic's content
format, and web search is Anthropic's server-side tool, whose version is picked from the model
name (`llm.web_search_type`). `EMBEDDING_MODEL` is `voyageai:<model>`. The vector column is
`vector(1024)` (`EMBEDDING_DIM` in `datastores/models.py`), and embeddings are requested at
that size, so `EMBEDDING_MODEL` must support 1024-dimension output. To use a different size,
change `EMBEDDING_DIM`, add a store tree migration that changes the column size, then re-ingest
documents.

## Deploy to AWS

```sh
cd infra/terraform
cp terraform.tfvars.example terraform.tfvars   # region, optional domain + ACM cert
terraform init
terraform apply                                 # VPC, RDS, Valkey, ALB, ECS, ECR, secrets

# Credentials stay out of Terraform state. Set them once:
aws secretsmanager put-secret-value --secret-id aios/external --secret-string '{
  "GITHUB_CLIENT_ID": "...", "GITHUB_CLIENT_SECRET": "...",
  "ANTHROPIC_API_KEY": "...", "VOYAGE_API_KEY": "..."}'

cd ../..
infra/deploy.sh          # build & push images, create roles, migrate, roll out (tag = git sha)
```

Services won't become healthy until the first `deploy.sh` has pushed images. Everything runs
in one region (`var.region`). RDS and Valkey sit in private subnets and only accept traffic
from the app security group. Without `certificate_arn` the site is served over plain HTTP on
the ALB hostname and session cookies aren't marked `Secure`. Set a domain and certificate for
anything real.

Database credentials are in three secrets (see [Database roles](#database-roles)):
`<name>/app`'s `DATABASE_URL` is `aios_app`'s, given to the api, the workers and migrations;
`<name>/provisioner` is `aios_provisioner`'s, for the `provisioning-worker` service and the
`migrate-stores` task only (their execution role is the only one that can read it); `<name>/app`
also holds `DATA_STORE_CREDENTIALS_KEY`, which encrypts data stores' passwords; and
`<name>/db-master` is the RDS master user's, for break-glass access. Only the `db-roles` task
(whose execution role is the only one that can read it) is given the master user's, to create the
roles before each migration; the secret also holds the two roles' DSNs, so that task reads nothing
else. On a stack with running services, `deploy.sh` creates the roles (a targeted apply of just
that task and secret, then the task) before the full apply switches `<name>/app` to `aios_app`, so
a task ECS starts in between never gets a role that doesn't exist yet. On a stack deployed before
these roles existed, the first deploy hands the schema to `aios_app`. Services still running the
previous release connect as the master user until they're replaced, which keeps working: the
master user is a member of `aios_app`.

### Sandboxes on Fargate (`run_python`)

On AWS, `SANDBOX_BACKEND=fargate` (`aios.sandbox.fargate`, `infra/terraform/sandbox.tf`): each
agent run that uses `run_python` gets its own Fargate task, so its own Firecracker microVM.

- **Starting**: the API calls `ecs:RunTask` on the sandbox task definition, passing the session's
  runner token, proxy URL and lifetimes as environment overrides, waits for the task to run and
  calls the runner (`serve` mode) at the task's private IP on port 8080. The runner exits when
  idle or at its maximum lifetime, which stops the task; the API also stops it if starting fails.
- **Network**: sandboxes run in subnets of their own whose route table has no default route.
  Their security group only admits the API (8080) and only lets them out to the egress proxy
  (3128), plus the ECR, CloudWatch Logs and S3 VPC endpoints Fargate needs to pull the image and
  ship logs. The S3 endpoint only allows ECR's layer bucket, and the interface endpoints only
  this account's credentials. RDS and Valkey don't admit sandboxes. The proxy is an ECS service
  (`egress-proxy`, API image) found at `proxy.<name>.internal` through Cloud Map.
- **IAM**: the sandbox task has no task role, so code in it has no AWS credentials. The API's
  task role (its own: the worker, web and migrate tasks share one with no permissions) may run
  only the sandbox task definition, in this cluster, and describe and stop the cluster's tasks.
- **Locally**, `docker compose` runs one `sandbox` container that every run shares
  (`SANDBOX_BACKEND=dev`). It has no isolation between runs and no network restrictions, so it's
  for development only. Allowed domains need no egress proxy there (`SANDBOX_PROXY_*` unset): the
  runner can reach every domain anyway.

Known limits:

- A sandbox takes tens of seconds to start. Starting it in the background when a turn starts
  hides most of that, but not all of it on a run's first turn.
- Each sandbox is one `ecs:RunTask` call (rate limited per account) and one IP in the sandbox
  subnets (a `/24` per AZ, so about 250 concurrent sandboxes per AZ).
- The runner token is a RunTask override, visible to anyone in the account with
  `ecs:DescribeTasks`.
- DNS lookups through the VPC resolver aren't filtered, so sandbox code can leak data in the
  names it looks up, and receive data in the answers. Route 53 DNS Firewall
  can't simply allowlist the names sandboxes need: its rule groups apply to a whole VPC, and
  the API, worker and egress proxy share this one and resolve any name (the proxy resolves
  whatever domains a run allows). Closing this means moving sandboxes into a VPC of their own
  (with their own endpoints, and the proxy and runner reached across VPCs), whose DNS Firewall
  allows only the proxy's Cloud Map name and the ECR, S3 and CloudWatch Logs endpoint names.
- The three interface endpoints cost about $45 a month across two AZs, plus data processing.

## Notes and trade-offs

- **Uploaded files are in S3** (`aios.datastores.files`; RustFS locally, in Compose and the
  tests), at `stores/<store id>/documents/<document id>`; the store's `documents` row keeps the
  key, the size and the SHA-256. An upload streams the file to S3, then inserts the row, then
  queues the ingest, and deletes the object again if either of those fails (unless the row is
  still there); the ingest reads it back from S3 (when S3 can't answer for now, the document
  waits, and the task tries again a minute later, up to ten times), and deleting a document
  deletes its object, then its row (so should S3 refuse, the document stays to be deleted
  again). Downloading
  (`GET /api/documents/{id}/download`) looks the document up in the user's own store, so
  another's is a 404, and redirects to a presigned `GetObject` link that saves it under its
  original name and lasts `DOCUMENT_DOWNLOAD_URL_SECONDS` (60). Anyone holding a link can use it
  until then, so one is minted per click and never shown on the page. Links are signed for
  `DATA_STORE_FILES_PUBLIC_ENDPOINT` when set (RustFS as the browser reaches it, while the app
  reaches it at `DATA_STORE_FILES_ENDPOINT`). On AWS the bucket (`<name>-data-<environment>`)
  has SSE-KMS, Block Public Access and no versioning (a delete really deletes); only the API's
  and the ingest worker's task roles may get, put and delete objects, under `stores/*`, and the
  app subnets reach it through an S3 gateway endpoint.
- **User data goes through a data store** (`aios.datastores`, the `data_stores` table): one per
  user, with an id of its own, created when they sign in. Document upload, listing, deletion,
  search, ingest and the document search tools open their sessions with
  `store_session(data_store_id, role)`; the run context (`ChatDeps`) and the ingest task carry
  the `data_store_id`, not the user. Each store is a database of its own, so isolating users'
  documents is the database's job, not a filter in every query.
- **Torch**: Docling needs `torch`, whose PyPI wheels pull in several GB of CUDA runtime.
  `pyproject.toml` locks torch/torchvision to PyTorch's CPU-only index instead, so installs
  (the worker image and a local `uv sync --extra ingest`) need access to `download.pytorch.org`
  and `download-r2.pytorch.org`.
- **Docker disk**: the worker image is still a few GB (torch CPU + Docling models). If a build
  fails with `read-only file system`, Docker's disk is full. Run `docker builder prune` and
  `docker system prune`, then restart Docker. On Docker Desktop, also raise the disk limit
  (Settings → Resources).
- Agent runs: each run is a LangGraph thread (`agent-run-<id>`) kept by
  the Postgres checkpointer, the client sends only its new message, and the agent continues
  from the thread's latest checkpoint. The user's message is checkpointed before the model is
  called; if a turn fails or is stopped, the reply's text so far (or an error result for each
  tool call that hadn't finished) is added to the thread, so the next turn sees what the user
  saw. Turns of the same run are serialized by a Postgres advisory lock: a second tab's turn
  waits for the first, then builds on it. `agent_runs` keeps a message count and title for
  listings, and the run's status, times and counters, written by each turn.
- **The checkpointer's tables are LangGraph's, not Alembic's.** They live in the `langgraph`
  schema, created and migrated by `AsyncPostgresSaver.setup()` (under an advisory lock) the
  first time a process runs a turn. Alembic only compares the `public` schema, so `alembic
  check` doesn't see them. Deleting a run or agent deletes its threads too (they aren't foreign
  keys, so this is done explicitly). The app's role (`aios_app`) can create that schema: it has
  `CREATE` on the database.
- **Tools are code, not data.** Each lives in `backend/src/aios/tools/`, registered under a stable
  key with a Pydantic settings model (every field has a default, and its JSON schema drives the
  settings form). An agent version stores which tools it has and their validated settings
  (`agent_version_tools`); each run builds them (LangChain tools, or provider tool definitions
  for server-side tools) and gives them to the agent. Tools read the user and run from the
  run's LangGraph context (`ToolRuntime[ChatDeps]`).
  To add a tool, write a module that calls `register(ToolSpec(...))` and import it in
  `aios/tools/__init__.py`. Never rename a key: old versions reference it. If a tool's behaviour
  changes in a way old versions shouldn't get, register it under a new key. A version whose tool
  has been removed from the library can still be viewed, but not restored or run. Stored
  settings are loaded leniently: fields the settings model has since dropped are ignored and new
  fields take their defaults, so changing a settings model doesn't break old versions. Saving,
  restoring and running all load them the same way, and refuse a value the model now rejects.
- **Web search** uses the model provider's native search tool (Anthropic's server-side web
  search), which is billed per search; its settings cap searches per model request and can
  allow or block domains. A model without native web search fails the turn rather than running
  without it.
- **Run Python** (`run_python`) lets an agent write and run Python in a sandbox: one per agent
  run, started when a turn starts (so it's usually ready by the first call) and reused by every
  call in the run, so files in `/work` carry over. The sandbox has no route to the database,
  Redis or the app's secrets. Its only way out is the egress proxy (`aios.sandbox.egress_proxy`),
  which allows HTTPS to the domains in the tool's settings and nothing else. The proxy token a
  sandbox is given carries those domains, signed with `SANDBOX_PROXY_SECRET`. The sandbox
  stops itself after `SANDBOX_IDLE_SECONDS` without a call, or `SANDBOX_MAX_SESSION_SECONDS` in
  total, and a later call starts a fresh one (the model is told `/work` was reset). The code
  runs in the `sandbox` image (`backend/Dockerfile`): the runner (`aios.sandbox.runner`, stdlib
  only) and the libraries pinned in `backend/sandbox/requirements.txt`. `SANDBOX_BACKEND`
  chooses how sandboxes are started; with none set, the tool tells the model it isn't
  available. See `plan/run-python/plan.md`.
- **Describe data** (`describe_data`) and **Query data** (`query_data`) read the run's data
  store, as its read-only role (`aios.tools.data`). `describe_data` lists the tables in `data`
  (the owner's) and the platform's `aios` tables, with their columns, types and `COMMENT ON`
  descriptions, from the catalog. `query_data` runs one SQL statement that returns rows, in a
  `READ ONLY` transaction that's always rolled back, under the role's statement timeout; the
  statement is sent with the extended protocol, which takes only one. Rows are streamed and cut
  off at `QUERY_DATA_MAX_ROWS` rows or `QUERY_DATA_MAX_BYTES` bytes (as JSON), with `truncated`
  set, and the session is reset (`DISCARD ALL`) before it goes back to the pool. Both tell the
  model their results are data, not instructions, and that the store is being set up until
  it's ready.
- **Request schema changes** (`request_schema_change`) is how an agent gets new tables, columns
  and indexes in `data`, or drops tables and columns (`aios.datastores.schema_changes`). The
  model sends a spec, never SQL: `create_table`, `add_column`, `create_index`, `drop_table`
  and `drop_column` operations, with types from an allowlist (including `embedding`, a vector
  of a text column that `write_data` fills) and names that
  match `^[a-z][a-z0-9_]{0,62}$` and aren't reserved words. The tool checks it against the
  store's schema, compiles it to SQL with every identifier quoted (descriptions become
  `COMMENT ON`), records a `schema_change_requests` row, and pauses the run with LangGraph's
  `interrupt()`: the turn ends with a card of the agent's reason and the exact SQL, and the run
  takes no new messages until it's decided. Any member of the store's organisation
  (`can_approve`, `resolve.approvers`) approves or rejects it (with an optional note) there or in the Data page's Changes section. Each user who
  can decide a new request gets a notification (`aios.notifications`, the `notifications`
  table: user, kind, a jsonb reference, created and read times), which the navigation's bell
  polls for (`GET /api/notifications`, unread first with an unread count, every 10s) and shows
  as an unread badge with a list; an item opens the request on the Data page, and one can be
  marked read (`POST /api/notifications/{id}/read`, idempotent). Deciding or cancelling the
  request marks its notifications read (`schema_changes.clear_notifications`). Users only
  ever see their own. An approved
  request is applied on the provisioning worker (`aios.apply_schema_change`), as the
  provisioner `SET ROLE` to the store's owner, in one transaction under
  `SCHEMA_CHANGE_LOCK_TIMEOUT` (5s), and appended to `aios.schema_history`, which bumps the
  store's data schema revision; if the revision moved on since the request was made, the spec
  is checked again, and one that no longer applies is marked `stale`. Then the workflow worker
  resumes the run (`aios.resume_agent_run`) with the outcome (applied, rejected with the note,
  failed with the error, or stale), which the tool call returns to the model, and the chat
  shows the rest of the turn. In a workflow run, the agent node's step becomes `waiting` (with
  the request it waits on); other branches carry on, and once nothing else can run the run
  becomes `waiting` and its task returns, holding no worker. A waiting run isn't failed for its
  heartbeat, and its waiting time (`waited_seconds`) doesn't count towards
  `WORKFLOW_RUN_TIME_LIMIT`. The decision queues `aios.run_workflow` again: the worker claims
  the run from `waiting`, rebuilds its state from the recorded steps, and resumes the waiting
  node on its own agent run's thread, on the versions the run pinned (a pin that can no longer
  run fails its step with the reason). The run page shows the waiting node with the request's
  card, and the pins that are no longer current. A request nobody decides within
  `SCHEMA_CHANGE_REQUEST_EXPIRY_DAYS` (7) expires: the provisioning worker's beat
  (`aios.expire_schema_change_requests`, every `SCHEMA_CHANGE_EXPIRY_CHECK_SECONDS`, an hour)
  marks it `expired`, which counts as a rejection, and the chat or workflow run that asked
  resumes with it. Deleting a chat run, an agent with a waiting chat, or a waiting workflow run
  marks its pending request `cancelled` instead, and nothing resumes (`schema_changes.cancel`).
  An agent whose workflow node waits can only be deleted by cancelling that workflow run, which
  cancels the request the same way (see deleting an agent, above). Either way the request's
  notifications are marked read, nothing is applied, and the Data page's history shows it.
- **Drops are reversible** (`aios.datastores.dropped`). `drop_table` and `drop_column` rename
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
- **Skills are inlined into the agent's instructions** (base prompt, then each pinned skill's
  description and instructions in a `<skill>` block). That's simple and works well for a handful
  of skills. With many large skills, switch to progressive disclosure: list only names and
  descriptions, and give the agent a tool to load a skill's full instructions.

## Tracing

Agent runs (the documents assistant's too) are traced to [Langfuse](https://langfuse.com) (open source,
self-hostable) through its LangChain callback handler, when both keys are set:

```sh
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_BASE_URL=https://langfuse.example.com   # your instance; default http://localhost:3000
LANGFUSE_ENVIRONMENT=production                  # optional
```

Each trace carries the user id, the run's thread id as the Langfuse session, tags for the agent
and version, and the run metadata above. A chat mention's handoff (see Chats) is traced too,
tagged `handoff`, with `invocation_id`, the chat, the message, who asked and the member as
metadata, and `chat-<id>` as the session. With no keys, nothing is traced. Traces are sent in the
background and flushed when the API shuts down.

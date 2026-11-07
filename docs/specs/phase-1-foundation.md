# Phase 1: Foundation

This spec covers plan steps 1.1–1.7: the pooler and turn lock, the store as system of record,
unified search and removing the export, retention, roles and the audit log, invocations with
usage and guards, and the run executor with a fair queue. It implements decision record 0001
(in `docs/decisions`), follows the phased plan (in `plan/platform-cohesion`), and uses the
vocabulary of the glossary (`CONTEXT.md`).

## Problem Statement

**Members of an organisation** can't rely on aios to hold all of their context in one place.
Their documents and data tables live in the organisation's own store. But their chats, the
briefs agents were given, the runs agents did, and the agents, skills, workflows and schedules
themselves all live in the shared platform database:
- An agent can't search what the team has said.
- Past runs are searchable only if someone switched on a separate copy (the run history export)
  and then a second switch to embed it.
- Searching documents, searching history and querying data are three different tools.

**Admins** have no real control. The organisation's creator is the only person with any special
powers, and those cover just renaming and invitations. Specifically:
- Any member can approve any schema change, including dropping a table.
- Nothing records who changed what.
- There's no way to decide how long runs and chat history are kept.
- There's no way to cap what an agent costs.

**Editors and participants** can't be told apart. Anyone who can use an agent can also rewrite
it. Letting people who should only use agents into an organisation is therefore unsafe, and so
is letting them mention agents: nothing bounds what one mention can cost.

**Everyone who chats with agents** shares a small, unfair pool of capacity:
- Agent turns run inside the web servers, five at a time per server.
- Workflow runs each hold one of two worker processes for up to two hours, though they spend
  nearly all of that waiting on the model.
- One organisation's burst, or a batch of schedules at nine o'clock, delays every other
  organisation's replies.
- A deploy stops turns mid-reply ("The service stopped mid-run.").

**Developers and operators** maintaining aios face a system whose boundaries are blurred:
- Organisation content is split across two databases.
- The turn lock depends on database session features that a connection pooler can't support,
  so the stores can't be pooled even though moving content into them multiplies connections.
- Usage is recorded only partially (handoff calls, embeddings, web searches and sandbox time
  aren't recorded at all).
- No limit stops a runaway turn.

## Solution

**The organisation's store becomes the system of record.**
- Everything the organisation makes, says and produces lives there: agents, skills, workflows
  and their versions, runs and their histories, turns, schedules, chats and their messages,
  invocations and briefs, approvals, the search index, the usage ledger and the audit log.
- The platform database keeps only who people are, which organisations exist, how to reach each
  store, people's notifications, and the small indexes needed to schedule and queue work across
  organisations.
- Conversation content is held where agents' own SQL tools can't read it, so a private chat
  stays private.

**One search covers everything.**
- Members and agents search documents, chat messages, past runs and briefs together, seeing
  only what they're allowed to see.
- The old document and history search tools keep working as narrower views of the same search.
- The run history export, and its settings, disappear.

**Admins govern the organisation:**
- They give each member a role: admin, editor or participant.
- They decide which roles can approve which kinds of schema change. Dropping data needs an admin
  by default.
- They read an audit log that the application itself can't alter.
- They choose how long runs and chat history are kept, with "keep indefinitely" as the default.
- They set limits on what a turn or a workflow run may cost.

**Editors build; participants use.**
- Editors create and change agents, skills, workflows and schedules. They can narrow who may
  invoke a given agent or workflow, and lower its limits.
- Participants chat, mention and start runs, using agents exactly as built.

**Every run has an invocation recording why it ran.** It might be a mention, a schedule, or a
person starting it by hand. Every billable call writes one row to a usage ledger, priced as an
estimate.

**Guards bound cost before anything is spent:**
- A cap on agents and workflows mentioned per message.
- A rate limit on mentions per person.
- Per-turn and per-workflow-run caps on model calls, tokens and cost.

All of these are generous by default, and all are configurable.

**Every turn and workflow node runs on a shared, elastic executor service:**
- It claims work from a queue that serves the organisation with the least in flight first, and
  people waiting in chat before background work.
- Workflows advance one node at a time, so a long workflow holds nothing while it waits.
- A deploy hands unfinished turns back to the queue, so they resume rather than fail.

## User Stories

### Storage and isolation

1. As a member, I want everything my organisation creates to live in our own store, so that our
   context is in one place we own.
2. As an admin, I want our chats, runs and agent definitions to sit in our store rather than a
   shared database, so that deleting or moving our organisation takes all of it with it.
3. As a member in a private chat, I want agents' SQL tools to be unable to read our
   conversation, so that private means private even inside our own store.
4. As an agent, I want to query reporting views of runs, turns, usage and the audit log, so that
   I can answer questions about how the organisation's agents perform without reading private
   conversations.
5. As an agent, I want a permission error, not data, when I try to read conversation tables
   directly, so that I learn to use search instead.
6. As a member in a direct message, I want no agent outside it, nor any agent's data tools
   anywhere, to be able to read or even list its messages, mentions, invocations, briefs or the
   runs it started, so that nobody outside a direct message can read it, not even through SQL an
   agent writes.
7. As a member, I want agents, workflows and runs I open to show who made them, even after that
   person has left, so that provenance survives departures.
8. As a member, I want someone who has left to appear as a former member rather than vanish, so
   that history stays readable.
9. As a member of two organisations, I want ids that repeat across organisations never to show
   me the other organisation's things, so that each organisation stays sealed.
10. As an operator, I want each domain moved into stores one release at a time, with writes to
   both databases until reads switch, so that a mistake never loses data.
11. As an operator, I want to switch a domain's reads back to the platform with one setting, so
   that a bad release can be undone without a data migration.
12. As an operator, I want a verifier that compares row counts and checksums between the
   platform and each store, so that I switch reads only when the copy is exact.
13. As an operator, I want backfills that can run twice safely, so that a crashed migration can
   simply be re-run.
14. As an operator, I want a reconciler that rebuilds derived copies (the people mirror, the
   schedule due index) from their source, so that a crash between two databases heals itself.
15. As a developer, I want a single rule for writes across databases (the source of truth writes
   first, and the derived copy catches up), so that no transaction ever spans both.
16. As a developer, I want the platform tables dropped only a release after their reads
   switched, so that every release works with the code before it.

### Pooler and turn lock

17. As an operator, I want stores reached through a connection pooler, so that moving all
   traffic into stores doesn't exhaust connections.
18. As a member with two tabs open on one run, I want my two messages to be answered one after
   the other, each seeing the previous, so that the conversation never forks.
19. As a member, I want a run that's busy for too long to tell me so, so that I'm not left
   waiting silently.
20. As a developer, I want the turn lock to be a lease on the run, refreshed by heartbeat, so
   that it works through a pooler.
21. As a developer, I want a process that lost its lease to be unable to write the run's
   history, so that a frozen process can't overwrite a newer turn.
22. As an editor archiving an agent, I want its turns underway stopped promptly, so that an
   archived agent stops acting.
23. As an operator, I want stopping a turn not to kill a shared pooled connection, so that
   stopping one turn never breaks another.

### Search

24. As a member, I want one search box that finds documents, chat messages, past runs and briefs,
   so that I don't have to know where something was said.
25. As a member, I want each result labelled with its kind and linked to its source, so that I
   can jump to the document, message or run.
26. As a member, I want to filter search by kind, so that I can look only at documents, say.
27. As a member, I want search to show nothing from private chats I'm not in, without hinting
   that hidden results exist, so that private chats leak nothing.
28. As a member, I want an option to see how the keyword and meaning searches ranked each
   result, so that I can understand surprising results.
29. As an agent, I want a search tool over everything in the store, with filters for kind, time,
   chat, agent and how many results, so that I can find any context I need.
30. As an agent in a private chat, I want my searches to include that chat's content, so that I
   can use what was said there.
31. As an agent started outside any chat, by a schedule or by hand, I want my searches to cover
   only organisation-wide content, so that I never surface a private chat.
32. As an editor, I want agents built with the old document search or history search tools to
   keep working unchanged, so that nothing I built breaks.
33. As a member, I want search to keep working by keyword when embeddings aren't available, so
   that an embedding outage doesn't blind search.
34. As an admin, I want the run history export and its settings gone, so that there is nothing
   to switch on to make history searchable.
35. As an operator, I want stores that had the export to have its copy dropped by a migration, so
   that no stale duplicate remains.
36. As an operator, I want embedding of new content to happen in the background under a budget
   per store, so that indexing never slows writing.

### Retention

37. As an admin, I want to choose how long runs are kept, or keep them indefinitely, so that our
   store follows our policy.
38. As an admin, I want to choose how long chat history is kept, covering private chats too, so
   that one policy governs conversations.
39. As an admin, I want "keep indefinitely" to be the default, so that nothing is deleted unless
   we decide.
40. As a member, I want a run that's still going, or waiting on an approval, never to be pruned,
   so that pruning can't break work in flight.
41. As a member, I want a message whose invocation is still running to be kept until it finishes,
   so that a reply always has its question.
42. As a member, I want a chat to remain after its old messages are pruned, so that the
   conversation can continue.
43. As an admin, I want documents, data tables, memory, usage and the audit log never pruned by
   these settings, so that retention affects only conversations and runs.
44. As an admin, I want each prune recorded in the audit log, with how many and up to when, so
   that pruning is accountable.
45. As an editor or participant, I want to see the retention settings read-only, so that I know
   the policy without being able to change it.
46. As an operator, I want pruning to run in small batches at a quiet hour, so that it never
   holds long locks.

### Roles and permissions

47. As an admin, I want every member to have one role (admin, editor or participant), so that
   what each person can do is clear.
48. As an organisation's creator, I want to become its admin, and everyone else an editor, when
   roles arrive, so that nobody loses abilities they had.
49. As an admin, I want to change a member's role on the Members tab, so that I can adjust
   access as people's jobs change.
50. As an admin, I want to choose the role when I make an invitation link, so that new people
   arrive with the right access.
51. As an admin, I want aios to stop me demoting the last admin, so that an organisation always
   has someone who can run it.
52. As a participant, I want to chat, mention agents and workflows and start runs from their
   pages, so that I can use what editors built.
53. As a participant, I want edit controls hidden or disabled with an explanation, so that I
   understand what my role allows.
54. As an editor, I want to create, edit, archive and unarchive agents, skills, workflows and
   schedules, so that I can build for the organisation.
55. As an editor, I want to upload and delete documents, so that I can curate what agents
   ground their answers in.
56. As a participant, I want to search and download documents, so that I can use them.
57. As an admin, I want only admins to manage roles, invitations, the organisation's name,
   retention, limits and deletion, so that organisation-wide decisions sit with admins.
58. As a developer, I want one permission check that every route calls, with one table of
   actions and roles, so that permissions can't drift between endpoints.
59. As a participant calling an editor's route directly, I want a clear refusal naming the
   action, so that the API enforces what the screen shows.

### Approval rules

60. As an admin, I want creating tables, adding columns and creating indexes approvable by
   editors, so that routine schema growth isn't bottlenecked on admins.
61. As an admin, I want drops, restores, purges and ownership changes to need an admin, so that
   destructive changes get senior eyes.
62. As an admin, I want to raise the role a kind of change needs, but never lower it below the
   default, so that I can tighten governance safely.
63. As an editor, I want a request mixing an added column and a dropped column to need an admin,
   so that a destructive part can't slip through with a harmless one.
64. As an approver, I want notifications only for approvals I'm allowed to decide, so that my
   bell isn't full of things I can't act on.
65. As a member who can't decide an approval, I want to see which role it needs, with Approve
   disabled, so that I know whom to ask.

### Audit log

66. As an admin, I want an audit log of configuration changes, approvals, archiving, role and
   membership changes, invitations, renames, settings changes, data writes and prunes, so that I
   can see who did what.
67. As an admin, I want to filter the audit log by actor, action and date, so that I can
   investigate quickly.
68. As an admin, I want the audit log to be impossible for the application to update or delete,
   so that it can be trusted.
69. As an admin, I want platform-side actions (role changes, invitations, renames, deletion
   scheduling) to reach the audit log even if the store was briefly unavailable, so that nothing
   goes unrecorded.
70. As an agent, I want to read the audit log through a reporting view, so that I can answer
   questions like "who changed this agent last week".
71. As an admin, I want each agent's write to a data table recorded with the table, operation,
   row count and run, so that data changes are traceable.

### Invocations

72. As a member, I want every run to record why it ran (a mention, a schedule, or someone
   starting it by hand), so that I can trace any run to its cause.
73. As a member, I want every run to record who caused it, or which schedule, so that cost and
   responsibility can be attributed.
74. As an operator, I want existing runs given invocations by backfill, so that history is
   consistent.
75. As a developer, I want invocations to carry a chain of their ancestors and a depth, so that
   later phases can stop loops.
76. As a member, I want each turn of a run recorded with its outcome, timings, counters and the
   agent version it ran on, so that turns, not whole runs, are what get counted and judged.
77. As a member, I want a run's status and counters derived from its turns, so that the run page
   and lists stay accurate.

### Usage ledger

78. As an admin, I want every model call, embedding, web search and second of sandbox time
   recorded, so that I can see what our agents cost.
79. As an admin, I want the handoff calls, search-query embeddings and document ingest embeddings
   counted too, so that nothing billable is missing.
80. As an admin, I want each usage entry attributed to its agent or workflow, run, turn,
   invocation, person and cause, so that cost can be broken down any way.
81. As an admin, I want each entry to carry an estimated cost from a configured price table, so
   that we see money, not just tokens.
82. As an admin, I want usage totals by agent, workflow, person and day for a date range, so
   that I can review spending before Insights exists.
83. As an operator, I want a model with no price to record a blank cost rather than fail, so that
   a missing price never breaks a turn.
84. As a developer, I want each kind of call to pass through one recording point, written in the
   same transaction as the turn's counters, so that retries never double-count.

### Guards

85. As a participant, I want to mention agents once guards exist, so that I can use agents like
   anyone else.
86. As a participant, I want my mentions refused with a clear message (rather than silently
   ignored) while guards aren't yet in place, so that I know why.
87. As a member, I want a message mentioning too many agents and workflows refused before
   anything is written, so that one message can't fan out without bound.
88. As a member, I want people I mention not to count towards that cap, so that I can address
   colleagues freely.
89. As an editor, I want to set who may invoke an agent or workflow (everyone, editors or
   admins), so that sensitive agents are kept to fewer people.
90. As a participant who mentions an agent I'm not allowed to invoke, I want my message posted
   with that mention's card explaining the refusal, while my other mentions run, so that one
   refusal doesn't block the rest.
91. As a participant, I want refused mentions not to count against my rate limit, so that I
   aren't penalised for something that didn't run.
92. As a member mentioning too often, I want my post refused with when to try again, and my draft
   kept, so that I can retry without retyping.
93. As an admin, I want per-turn caps on model calls, tokens and cost, so that a runaway turn
   stops itself.
94. As an admin, I want per-workflow-run caps on model calls and cost, so that a looping workflow
   stops itself.
95. As a member whose turn hits a cap, I want the turn to end with a message naming the limit,
   keeping what the agent got to, so that I understand what happened.
96. As an editor, I want to lower an agent's or workflow's limits, so that I can keep a risky
   agent cheap.
97. As an editor, I want to be refused when I try to raise a limit above the organisation's,
   with a message saying only admins can, so that cost ceilings are an admin decision.
98. As an admin, I want to override the deployment's default limits for my organisation, so that
   our ceilings fit our needs.
99. As an operator, I want every limit to be a setting with a generous default, so that real
   values can be chosen later without code changes.
100. As a developer, I want the checks in a fixed order (count, permission, rate limit, budget,
   handoff), so that no refused mention ever costs a model call.
101. As a developer, I want schedules and manual starts to skip the mention checks but still be
   capped and charged, so that automation is bounded too.

### Executor and fair queue

102. As a member mentioning an agent, I want my turn to start promptly even while another
   organisation floods the system, so that my chat stays responsive.
103. As a member, I want chat work served before background work, so that people waiting come
   first.
104. As an admin, I want my organisation to have a fair share of capacity, so that a burst from
   us waits on our own share rather than everyone's.
105. As an admin, I want to be able to override our in-flight cap, so that heavier organisations
   can be given more room.
106. As an editor, I want a long workflow to make progress without holding a worker for hours,
   so that many workflows can run at once.
107. As an editor, I want independent branches of a workflow to run at the same time, so that
   workflows finish sooner.
108. As an editor, I want subworkflows to run as part of their parent's progress, so that nesting
   costs nothing extra.
109. As an editor, I want a workflow waiting on an approval to hold nothing and resume as soon as
   it's decided, so that waiting is free.
110. As an editor, I want a workflow run's time limit to count active time only, not time spent
   waiting, so that approvals don't time runs out.
111. As a member, I want a deploy mid-reply to resume my turn from where it was, rather than fail
   it, so that releases are invisible to me.
112. As a member, I want a turn whose executor crashed retried elsewhere, and failed with a clear
   message only after several attempts, so that one crash doesn't lose my request.
113. As a member on a run page, I want my turn to carry on if I close the tab, and to see it when
   I return, so that leaving doesn't waste work.
114. As a member on a run page, I want replies to keep streaming as they do today, so that moving
   turns off the web server is invisible.
115. As an operator, I want web servers to serve requests only, with no agent turns in them, so
   that a burst of turns never slows pages.
116. As an operator, I want one executor service, many turns per process, so that I/O-bound work
   doesn't need a process each.
117. As an operator, I want executors woken when work arrives rather than polling, with a slow
   poll as a fallback, so that latency is low and the database quiet.
118. As an operator, I want schedules claimed and enqueued by executors, so that firing is part
   of the same fair system.
119. As an operator, I want a shared limiter per model across all executors, so that we stay
   within the model provider's rate limits.
120. As an operator, I want an executor whose event loop stalls to exit so that its work is
   retried elsewhere, so that one stuck process can't hold work forever.
121. As an operator, I want the old workflow worker and its queue removed once executors run
   everything, so that there's one way to run work.
122. As an operator, I want a temporary switch back to the old path for the release that
   introduces executors, so that the cut-over can be undone.
123. As a developer, I want enqueueing to be idempotent by a deduplication key, and a reconciler
   to re-enqueue invocations left queued without work, so that a lost enqueue repairs itself.
124. As a developer, I want a "drain the queue once" entry point, so that tests can run the
   executor deterministically.

## Implementation Decisions

### Store layout and roles

- **A new private schema.** Each store gains a schema, called `app`, for everything the platform
  writes on the organisation's behalf, plus its own schema for LangGraph checkpoints.
  - Neither is readable or writable by the store's read-write or read-only roles. Those roles
    sit behind the data tools and the Data page's tables.
  - Today, default privileges grant those roles access to every table the store's owner creates,
    in every schema, and the data-description tool lists the platform's own store schema. Moving
    conversations there without this change would let model-written SQL read private chats.
- **A new login role per store, `_app`.** The platform uses it for the private schema and the
  checkpoints. Its password is generated, encrypted and kept on the store's registry row, like
  the other roles'. Provisioning creates it, and the store migration command creates it for
  existing stores.
  - Default privileges in the private schemas grant to `_app` only, with explicit revokes from
    the other roles.
  - The store-session helper gains an `app` role value.
- **The rule.** Conversation content lives only in the private schemas. That covers chats,
  direct messages, messages, mentions, invocations, briefs, run messages and checkpoints, and
  memory once phase 3 adds it.
  - The roles behind the data tools and the Data page's Tables view can neither read these
    schemas nor describe them. The data-description tool and the Tables view list only the
    `data` schema, plus platform tables explicitly allowed (documents and their chunks, as
    today, and the reporting views).
  - The platform's code reaches conversation content only as `_app`.
  - Agents reach it only through the search tool, which enforces visibility.
  - Phase 3's per-agent database roles inherit this rule unchanged: no agent role is ever
    granted the private schemas.
- **A reporting schema** of read-only views is granted to the read-only role, so agents can
  query it. It holds the audit log, usage, runs and turns, as identifiers, counts, times and
  statuses only. It never includes message text, briefs or tool output, and leaves out every
  row that concerns a private chat or direct message. Audit entries keep only their action,
  actor, target and time; their free-form details stay in the private schema.

### Moving domains into the store (step 1.2)

**Order.** Each domain is one release:
1. a people mirror;
2. agents and skills, with all their versions, skill pins and tool settings;
3. workflows, with versions, nodes and edges;
4. runs: agent runs, sandbox sessions, workflow runs, steps and checkpoints;
5. triggers;
6. chats: chats, chat members, messages and invocations;
7. schema change requests.

**Mechanics.** Each domain goes through the same steps:
1. **Expand:** a store migration creates the tables.
2. **Dual-write and backfill:** the platform writer calls a store mirror after its own commit.
   The backfill upserts by id, keeping ids. Each store sequence is then set past the highest id.
3. **Verify, then switch reads:** a verifier compares row counts and checksums per store. Reads
   switch by a per-domain setting, so they can be switched back.
4. **Contract:** a later release drops the platform tables.

**Ids.** Ids are unique within an organisation afterwards, and may repeat across organisations.
So everything that names a row outside its store carries the store or organisation too:
- task arguments;
- Redis channel names;
- notification references, which gain the organisation, backfilled;
- trace metadata. Trace session names become `agent-run-<organisation>-<id>`; old traces keep
  their names.

**Cross-database writes.** No transaction spans both databases:
- **The source of truth writes first.** The platform is the source for people, roles and
  notifications. The store is the source for everything moved.
- **Derived copies** are written afterwards, idempotently, and a reconciler on the maintenance
  worker rebuilds them every ten minutes and at the end of the store migration command. There
  are two:
  - the store's people mirror (person id, names, email, role, joined and left times);
  - the platform's schedule due index (trigger, organisation, store, next run time).

**Provenance.** Who made, changed, archived or added something becomes a plain person id,
resolved through the people mirror. Someone who has left shows as a former member.

**Checkpoints.** The checkpointer connects per store, as `_app`, through the pooler. Its setup
runs per store on first use. Threads keep their names.

**Schedules.** Firing re-reads the trigger in the store under a row lock, and drops the firing
if the trigger was disabled or edited, as today's cancel-unstarted-firing rule does.

### Pooler and turn lease (step 1.1)

- **The pooler.** PgBouncer in transaction mode sits in front of the stores, as a new Compose
  service and a new ECS service.
  - RDS Proxy was rejected: it pins sessions that use advisory locks or session settings, and
    doesn't offer per-database pools.
  - The platform database stays directly connected.
- **No session state on store connections:**
  - no session advisory locks;
  - no prepared statements, on every engine and on the checkpointer;
  - no settings passed at connection startup. The checkpointer's search path becomes a role
    default.
  - The checkpointer's setup lock becomes a lock scoped to one transaction.
- **The turn lock becomes a lease on the run's row:**

  | Part | How it works |
  | --- | --- |
  | Columns | A token, and a time the lease lasts until. |
  | Acquire | A conditional update that succeeds only if the lease is free or expired. |
  | Wait | On a Redis release channel for the run, with a one-second poll as a fallback, for up to a configurable time. The request then answers 409 "This run is busy", or an executor re-queues the item. |
  | Refresh | Extended by the heartbeat while the token matches. |
  | Release | Cleared while the token matches, then the release channel is published. |
  | Fencing | Every checkpoint write first checks that the token still holds, and fails if it doesn't. |
  | Stopping turns | Clears the token and publishes a stop signal that the holding process acts on by cancelling the turn. Terminating the holder's database backend goes, since through a pooler it would kill a shared connection. |
  | "Turns underway" | Reads unexpired leases. |

- **Pool sizes.** Per-process store pools become one connection plus four overflow. Pooler pools
  replace the per-role connection limit, which stays as a backstop, raised to 100.

### Unified search (step 1.3)

- **The index.** A search item table in the store. Each item records:
  - its kind: document chunk, chat message, run message or brief. Later phases add memory and
    synced source items;
  - a reference to its source row, plus extra reference data;
  - its text, a generated full-text vector and a nullable embedding;
  - a hash of the embedded text, so unchanged text isn't re-embedded;
  - when it was written;
  - its visibility: either the whole organisation, or one chat's chat members (private chats
    now, direct messages from phase 2);
  - the agent, chat, run and author, for filters.
- **Writing.** Each writer indexes in the same transaction as its row:
  - ingest indexes chunks;
  - posting a message indexes it;
  - ending a turn indexes the user's and assistant's text, not tool results;
  - the handoff indexes its brief.

  Embedding is deferred to background work items, under a time budget per store per sweep.
  Search falls back to keyword-only for rows not yet embedded.
- **Visibility.**
  - A run's items and a brief take the visibility of the chat their invocation came from.
  - Runs with no chat, and documents, are organisation-wide.
  - Agents see organisation-wide items, plus the items of the chat their invocation came from.
  - People see organisation-wide items, plus items of private chats they're in.
- **The `search` tool** (new tool key). It takes a query, and optionally kinds, a time range,
  chat, agent and a result count. Its settings are the maximum results (default 8, up to 50) and
  the kinds allowed.
  - The document search and history search keys stay registered, as the same search fixed to
    their kind, with their result shapes unchanged.
- **The Search page's endpoint** returns the signed-in person's visible items, with kind filters
  and a 1–50 limit. Today's explanation of how each search branch ranked a result moves behind a
  "Show ranking" toggle.
- **Removing the export.** This ships a release after history search reads the new index. It
  removes:
  - the export module, its outbox table, its three tasks and their beat entry;
  - the export and embedding columns on the store registry;
  - the export fields of the organisation settings endpoint, and the delete-exported-history
    endpoint;
  - the export panel.

  A store migration on the history branch drops the history schema. The branch's revisions stay,
  so a store at any of them can migrate.

### Retention (step 1.4)

- **Settings.** Two nullable settings on the organisation, held in the platform database: run
  retention days and chat retention days, from 1 to 36,500. Null means keep indefinitely, which
  is the default. Only admins set them.
- **The pruning job.** A daily job, per store with a setting, on the maintenance worker, at a
  configurable hour, deleting in batches of 1,000.
  - **Runs** go with their steps, turns, sandbox sessions, checkpoint threads and index items.
    Never pruned: runs running, queued or waiting, or waiting on an approval. Phase 1 prunes
    whole runs; phase 3 adds turn-by-turn pruning of continuing runs.
  - **Messages** go with their mentions, invocations, briefs and index items. Never pruned: a
    message whose invocation is unfinished.
  - **Never touched:** documents, data tables, memory, usage and the audit log.
- **Audit.** One audit entry per kind per run of the job.

### Roles, approval rules and the audit log (step 1.5)

- **Roles.** A role enum on memberships, backfilled: creators become admins, everyone else
  editors. The creator-only checks go. Invitations carry a role, editor by default.
- **One permission module.** It answers "can this member do this action" from one table, and
  raises a 403 naming the action. Every route calls it.

  | Action | Admin | Editor | Participant |
  | --- | --- | --- | --- |
  | Read and search what they can see | ✓ | ✓ | ✓ |
  | Post, mention, and start runs (from a page too) | ✓ | ✓ | ✓ |
  | Create, edit, archive and unarchive agents, skills, workflows and triggers | ✓ | ✓ | |
  | Upload and delete documents | ✓ | ✓ | |
  | Share and transfer tables (phase 3) | ✓ | ✓ | |
  | Roles, invitations, the organisation's name, retention, limits, default chat agents (phase 2), connectors (phase 4), deleting the organisation | ✓ | | |
  | Remove people from chats, archive chats (phase 2) | ✓ | | |
  | Read the audit log and usage totals | ✓ | | |

- **People and roles in the API.** "Who am I" returns each organisation with the person's role.
  A new endpoint changes a member's role. Demoting the last admin answers 409.
- **Participants' mentions.** Until step 1.6 ships, a participant's mention answers 403 with a
  message that mentions are coming.
- **Approval rules.** A store table maps each change kind to the least role that may decide it.

  | Change kind | Least role |
  | --- | --- |
  | create table, add column, create index | editor |
  | drop table, drop column, restore, purge, change ownership | admin |

  - Admins may raise a kind's role but not lower it below the default.
  - A request needs the highest role among its operations.
  - The approver lookup and notification fan-out read these rules and roles.
  - New endpoints read and update the rules.
- **The audit log.** A store table that's append-only. The `_app` role may only insert and read,
  and a trigger refuses updates and deletes even for the owner. Each entry records:
  - the time;
  - the actor's kind (person, agent or system), with the person, agent and run;
  - a dotted action name, such as "agent.archived" or "approval.decided";
  - the target's kind and id;
  - details.

  Actions taken on the platform side go first to a platform outbox table, in the same
  transaction, and are drained into the store every ten seconds, idempotently by outbox id.
  - Admins read the log through a filterable, cursor-paged endpoint, and on a new Audit log tab
    of the Organisation page.
  - Agents read it through the reporting view.

### Invocations, usage and guards (step 1.6)

- **Invocations.** Chat invocations become general store invocations, created in step 1.2 with
  a cause column defaulting to mention.

  | Group | Fields |
  | --- | --- |
  | Cause | mention, schedule, manual (start workflow and event arrive in phase 3) |
  | Who or what caused it | the person, the trigger, the parent invocation |
  | Loop prevention | a chain of ancestor ids, and a depth |
  | Target | agent or workflow |
  | Where from | chat and message, if any |
  | Delivery | a deliver-to chat, set by phase 2 |
  | Status | queued, briefing, running, succeeded, failed or refused, with a refusal reason and an error |
  | Handoff | acknowledgement and brief |
  | Results | the agent run or workflow run |
  | Timings | created, started and finished |

  - **Who creates them:** posting a mention; firing a schedule; starting a run from a page and
    posting each turn to it; and each workflow agent node, as a child of its workflow run's
    invocation.
  - **Backfill:** existing runs become schedule, mention or manual, as their records show.
- **Turns.** A turn table records, per turn:
  - its run, invocation and agent version;
  - its status and error;
  - its timings;
  - its model calls, tool calls, input tokens and output tokens.

  A run's status, times and counters derive from its turns. The run's own columns are written
  alongside until a later contract.
- **The usage ledger.** A store table with one row per billable unit (a model call, an embedding
  batch, a web search count, or sandbox seconds). Each row records:
  - the model, the token counts (including cache reads and writes) and a quantity;
  - an estimated cost;
  - the purpose: turn, handoff, handback, compaction, ingest, search query, index embedding or
    insight;
  - the agent, workflow, run, turn, invocation, the person charged, and the cause.

  **Recording points.** One for each kind of call:
  - a callback on every chat model, covering turns and the handoff;
  - web search counts, read from the model's response;
  - the embeddings module, once per batch;
  - the sandbox, when a session ends.

  Rows are written with the turn's counters, in one transaction.

  **Prices** come from a configured price table, keyed by model. A model with no price records a
  null cost.

  **Reading usage.** An admin endpoint returns totals by agent, workflow, person and day.
- **Mention guards.** These run at posting, in this order:
  1. **Count.** Distinct agents and workflows mentioned, at most five by default. Over that, the
     post answers 422 and nothing is written. People mentioned don't count.
  2. **Permission, per mention.** The poster's role, and the target's **invocable by** (a new
     field on agents and workflows: everyone by default, editors or admins). A failure posts the
     message with that mention's invocation refused and the reason on its card, and makes no
     model call.
  3. **Rate limit.** Allowed mentions are counted per person per organisation in Redis (60 a
     minute by default). Over that, the whole post answers 429 with a time to retry, and nothing
     is written.
  4. **Budget.** A no-op until phase 3.
  5. **Handoff.**

  Schedules and manual starts skip the count and rate checks.
- **Caps.** A per-turn limit callback counts model calls, tokens and estimated cost, and ends the
  turn before a call that would pass a cap.

  | Cap | Default |
  | --- | --- |
  | Model calls per turn | 200 |
  | Tokens per turn | 5 million |
  | Cost per turn | 25 |
  | Model calls per workflow run | 2,000 |
  | Cost per workflow run | 100 |

  - LangGraph's own recursion limit is set from the model-call cap.
  - Overrides live on each agent and workflow (editors may lower; only admins may raise above
    the organisation's), and on the organisation (admins only, in a limits field held in the
    platform database).
  - A turn that hits a cap fails with a message naming the limit, keeping its partial reply.

### Executor and fair queue (step 1.7)

- **The queue.** A work item table in the platform database. Each item records:
  - its organisation and store;
  - its lane: interactive (mentions and run-page turns) or background (schedules, workflow
    nodes and advances, resumes, embedding sweeps). The remaining lanes come in phase 3;
  - its kind: invocation start (the handoff, then the first turn or advance), agent turn, resume
    turn, workflow node, workflow advance, or embedding sweep;
  - a payload of store ids;
  - when it became ready, and when it was enqueued;
  - its attempts and lease;
  - an optional unique deduplication key.

  Items are deleted when done; their outcome lives in the store.
- **Enqueueing** happens after the store commit. Executors are woken through a Redis channel per
  lane, with a five-second poll as a fallback. A reconciler re-enqueues invocations left queued
  without a work item for over a minute.
- **Claiming.** One transaction, in two stages:
  1. **Choose candidates.** Count items in flight per organisation. Rank each organisation's
     ready items by readiness, and keep only as many as its cap allows. Order the candidates by
     interactive lane first, then by fewest in flight, then by oldest.
  2. **Lease them.** Lock and lease the candidates still free, skipping locked rows.

  - The per-organisation cap (default 20, overridable by admins) is soft: concurrent claimers
    can overshoot it by the few items each claims. This was accepted over serialising claims.
  - Each executor reserves 10 of its 50 slots (both configurable) for the interactive lane.
- **Leases and retries.** Leases last 90 seconds and are extended every 30. An expired lease
  makes the item claimable again. After three attempts, the item fails its invocation, turn or
  step with "The service stopped mid-run."
- **The executor service.** A new service on the API image. Each process runs one event loop
  with a concurrency limit. It handles each kind of work as follows:

  | Kind | What it does |
  | --- | --- |
  | Invocation start | Runs the handoff, which moves out of the API, then enqueues the first turn or advance. |
  | Agent turn, resume turn | Runs the turn under the run's lease, through the existing turn code. |
  | Workflow advance | A short coordinator step under the workflow run's row lock (below). |
  | Workflow node | An agent node's turn, as a child invocation. It enqueues an advance (deduplicated by run) when done. |

  A **workflow advance**:
  1. rebuilds state from the recorded steps (as resume does today);
  2. checks the time limit, as active time excluding waiting;
  3. records finished nodes, and works out which are ready;
  4. starts their steps and enqueues them;
  5. ends the run when nothing is ready and nothing is running.

  Subworkflow nodes are expanded within the advance.
- **The scheduler.** Moves into executors. Any executor claims due schedule-index rows, skipping
  locked ones, moves them on, creates the invocation in the store, and enqueues it.
  - The firing machinery's launch and cap, the chat turn launching, and the scheduler task in
    the web server's lifespan are removed.
  - The scheduler-enabled setting now applies to executors.
- **What goes.** The workflow run task, the resume task, their Celery queue and the workflow
  worker service (Compose and Terraform), along with per-run workflow heartbeats.
- **The no-progress limit** becomes an executor watchdog: the process exits if its loop lags past
  it.
- **Shared model rate limit.** A token bucket per model in Redis limits requests and tokens per
  minute across executors. It's unset (no limit) by default. Waiting calls are served in the
  same order as claims.
- **Graceful shutdown.**
  1. On a termination signal, stop claiming.
  2. Let items finish for up to 25 seconds.
  3. Cancel the rest. A cancelled agent turn keeps what it got to and is re-queued as a resume
     that continues from its last checkpoint, without counting as an attempt.
- **The run page's chat** (removed in phase 2) enqueues an interactive turn. It relays the
  executor's output to the browser through a Redis stream, in today's streaming format. The
  turn continues if the browser disconnects.
- **Test entry point.** A "drain the queue once" entry point runs every ready item to completion
  in-process.

### API contracts

All are under the organisation-scoped prefix from phase 0.

| Endpoint | Change |
| --- | --- |
| Search | Kinds, a limit, and results carrying kind, text, reference, chat, run, time and ranks. |
| Organisation settings | Loses the export fields. Gains run retention, chat retention and limits. Writable by admins only. |
| Delete exported history | Removed. |
| Members | New role update, refusing to demote the last admin (409). |
| Invitations | Take a role. |
| Audit log | New, admins only, filtered and cursor-paged. |
| Approval rules | New, read by members and updated by admins (422 if a role is lowered below the default). |
| Agents and workflows (update) | Accept **invocable by** and limits (403 when a non-admin raises a limit). |
| Posting a message | 422 for too many mentions, 429 for the rate limit. Refused invocations come back with their reason. |
| The run page's chat | Same stream format, now relayed from an executor, with 409 when the run stays busy. |
| Usage | New, admins only: totals grouped by agent, workflow, person or day. |
| "Who am I" (not organisation-scoped) | Returns each organisation's role. |

Every route returns 403 for actions the member's role doesn't allow.

### Settings (all new unless noted; all generous by default)

| Group | Settings |
| --- | --- |
| Pooler and leases | Pooler address (empty means direct); per-role connection limit (changed: 100, as a backstop); turn lease 90 seconds; turn wait 120 seconds. |
| Store migration | Per-domain read switches (off until flipped); reconciler interval 600 seconds. |
| Search | Embedding budget per store per sweep, 300 seconds; maximum search results, 50. |
| Retention and audit | Pruning batch 1,000; pruning hour 03:00 UTC; audit drain interval 10 seconds. |
| Usage | Price table, a JSON map from model to prices, covering the deployment's current models. |
| Guards | Mentions per message, 5; mention rate per person, 60 a minute. |
| Turn and run caps | Per turn: 200 model calls, 5 million tokens, a cost of 25. Per workflow run: 2,000 model calls, a cost of 100. |
| Executor | Concurrency 50; interactive reserve 10; idle poll 5 seconds; shutdown grace 25 seconds. |
| Queue | Lease 90 seconds; maximum attempts 3; organisation in-flight cap 20; target time to start a chat turn, 5 seconds. |
| Model rate limits | Requests and tokens per minute, per model, unset by default. |
| Executor cut-over | A temporary switch to run turns in the API, for the cut-over release only. |
| Removed | The per-process concurrent-runs cap (replaced by executor concurrency). |
| Repurposed | The no-progress limit (now the watchdog) and the scheduler-enabled switch (now for executors). |

### Rollout order (one release per line)

1. Pooler, with the lease taken alongside the old advisory turn lock.
2. Lease only; the advisory turn lock and backend termination are removed.
3. The private schema and `_app` role on every store, then the people mirror.
4. Agents and skills.
5. Workflows.
6. Runs, with their checkpoints.
7. Triggers, with the due index.
8. Chats.
9. Approvals.

   For each of 4–9: dual-write, backfill, verify, switch reads. The whole sequence is rehearsed
   on a production copy first.
10. The search index and its backfill, the search tool, the Search page.
11. Removal of the export; retention.
12. Roles, permissions, approval rules and the audit log, with participants' mentions blocked.
13. Invocations, turns, usage and guards; participants' mentions allowed.
14. Executors alongside the old path, with the temporary switch back.
15. The old path, the workflow worker, and the platform tables of each moved domain are dropped,
   one per release once its read switch has held for a full release; then the heartbeat
   columns.

## Testing Decisions

**What makes a good test here.** It exercises behaviour a person, agent or operator can observe,
through the highest seam that reaches it: what an API call returns, what a member can or can't
see, what a tool returns, which rows a store holds after a migration command. It doesn't assert
on internal helpers, SQL text or call order inside a module.

Cross-database behaviour is tested by its outcome:
- after a backfill, both databases agree;
- after a crash between the two writes, the reconciler repairs it.

Security properties get adversarial tests that try to break them.

**The seams**, in order of preference:

1. **The HTTP API, called as a signed-in member** through the test client, against real
   Postgres, Redis and S3-compatible storage, with the scripted model in place of the provider.
   This is the primary seam, and most of the backend suite already works this way. It covers:
   - permissions for every route and role, parametrised over one table;
   - approval rules and notification fan-out;
   - search results and visibility, and the Search page's endpoint;
   - retention settings;
   - guards (422, 429, refused cards, caps, and limit overrides by role);
   - usage totals, the audit log endpoint, and invocations created for each cause.
2. **The full-stack browser test from phase 0**, extended rather than duplicated. The core loop
   (mention, schema change, approval, resume, reply) now runs on executors through the pooler,
   and also checks that:
   - a usage row exists per model call;
   - the approval needed an editor;
   - the audit log recorded the decision.

   A second scenario restarts the executor mid-turn and expects the turn to resume.
3. **Fake-API browser tests** for screens:
   - the Search page's kinds and ranking toggle;
   - retention and limits settings as admin, editor and participant;
   - roles on the Members tab and on invitations;
   - the audit log tab;
   - refused-mention cards and the composer's 422 and 429 messages;
   - **invocable by** and limits controls;
   - the export panel gone.

   Prior art: the existing Playwright specs.

**Narrower seams**, only where the API can't reach:

- **The new "drain the queue once" executor entry point.** It covers:
  - claim fairness;
  - the in-flight cap;
  - interactive work first;
  - lease expiry and retry;
  - the attempt limit;
  - graceful hand-back and resume;
  - workflows advancing node by node, with parallel branches, subworkflows, waiting and
    resuming, and the active-time limit;
  - the starvation test: one organisation enqueues a thousand background items, and another's
    interactive item starts within the target.

  Prior art: the workflow run tests that call the run entry point directly. The firing,
  scheduler, workflow run, workflow pause and chat invocation tests are ported to it.
- **Tool invocation**, for:
  - the search tool, with the old keys and visibility from a run's context;
  - the data tools being refused on private schemas and allowed on reporting views.

  Prior art: the data tools tests.
- **The store migration command**, for:
  - backfills run twice;
  - the verifier;
  - the reconciler;
  - the history schema's drop from every history revision;
  - `_app` grants.
- **The turn lease**, through two concurrent turns on one run via the API, with a lease-expiry
  case that proves a stale holder can't write.
- **CI configuration.** The suite runs against PgBouncer as a service container.

**Adversarial tests:**
- `query_data` and `describe_data`, run by an agent in an organisation-wide chat, must get a
  permission error or an empty listing for:
  - a direct message's messages;
  - a run's messages and checkpoints;
  - briefs, mentions and invocations;
  - the audit log's details;
  - catalogue tricks: querying the system catalogues for the private schemas, switching role
    through session settings, and querying the private schemas directly.

  The same SQL through the reporting views must return no rows about the direct message and
  no message text.
- model-written SQL trying to switch role through session settings;
- a stale lease holder writing a checkpoint;
- a participant calling editor routes directly;
- over-limit mentions posted around the UI;
- updates and deletes against the audit log as the app role.

## Out of Scope

- **Phase 2:**
  - ownerless chats and direct messages;
  - streaming chats over server-sent events (phase 1 keeps chat polling, and only adds the
    Redis relay the run page needs);
  - posting messages from agents, deliver-to, handback and the new notification kinds;
  - export and import.
- **Phase 3:**
  - continuing runs per chat, and taking the current version each turn (phase 1 records the
    version per turn but keeps runs pinned);
  - turn-by-turn pruning of continuing runs;
  - memory;
  - per-agent database roles;
  - approval nodes, and merging schema change requests into approvals;
  - organisation budgets and allowances (the budget check is a placeholder);
  - event triggers;
  - the full set of lanes, and autoscaling.
- **Phase 4:** sources and connectors, and the Insights page (it reads the ledger built here).
- **Roadmap:** billing, real limit values, single sign-on, and teams inside an organisation.

## Further Notes

**Dependencies on other specs:**
- Phase 0's spec (`phase-0-refactor-and-guard-rails.md`) must land first. Phase 1 needs:
  - the organisation-scoped routes, because ids repeat across organisations after step 1.2;
  - archiving, because the store's agent tables carry archive fields;
  - the split modules, the full-stack harness and sharded CI.
- Phase 2 (`phase-2-organisation-experience.md`) generalises this phase's Redis relay to chat
  streaming. It turns private chats into direct messages, using the same visibility field, and
  fills in the deliver-to field and the remaining notification kinds.
- Phase 3 (`phase-3-agents-and-automation.md`) inherits the rule that no data-tool role (and
  so no per-agent role) can read or describe the private schemas. It also fills in:
  - the budget check;
  - the start-workflow and event causes;
  - turn-level pruning;
  - the remaining lanes;
  - per-agent roles, which build on the private-schema split decided here.
- Phase 4 (`phase-4-inflow-and-insight.md`) reads the usage ledger and reporting views.

**Risks:**
- **Data loss while moving domains.** Mitigated by dual-writes, backfills that can be re-run,
  the verifier, read switches that can be undone, contracts a release later, and a rehearsal on
  a production copy.
- **Connection exhaustion.** Mitigated by putting the pooler in first.
- **Private content leaking through the data tools.** Mitigated by the private schema, explicit
  revokes and adversarial tests.
- **Starvation.** Mitigated by fair claiming, the interactive reserve and the starvation test.

**Departures from the ADR and plan, decided here:**
- The ADR places conversation tables in the store's existing platform schema. Today that schema
  is readable by the data tools, so this spec adds a private schema and a dedicated role. That
  follows the ADR's privacy decisions rather than changing them.
- The ADR names RDS Proxy as an option for pooling; this spec chooses PgBouncer.
- The plan keeps the scheduler "only to enqueue"; this spec moves it into executors.

**Decided after review** (these were this spec's open questions)
1. **Documents:** participants may view and search documents. Only editors and admins upload or
   delete them.
2. **The in-flight cap is soft.** Executors claiming at the same moment may overshoot it by a few
   items. Fairness doesn't need it to be exact, and serialising claims would slow every
   organisation.
3. **Chat text reaches agents only through `search`,** never through reporting views.
4. **Run-page turns** are still abandoned when the browser disconnects, until phase 2 removes that
   chat.
5. **Currency:** estimated costs and budgets are in pounds sterling (GBP). The price table is
   configured in GBP per model; an operator converts provider prices when setting it.

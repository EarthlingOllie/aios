# Agents and skills

An **agent** is a base prompt plus a set of **skills** (reusable instructions) and **tools** from
a built-in library (see [Tools](tools.md)), and you chat with it. Each conversation with an
agent is a **run**. Agents are LangGraph agents (LangChain's `create_agent`, on Anthropic).
Agents, skills and the runs of agents belong to the organisation (see
[Accounts and organisations](accounts-and-organisations.md#what-the-organisation-owns)).

## The Agents and Skills lists

The Agents and Skills lists each have a search box: every word typed must appear in an item's
name or description, ignoring case and accents, and the list shows how many of its items match
("3 of 12"). The search is kept in the URL (`/agents?q=web`), so a reload, a shared link or Back
keeps it, and opening, editing, creating or archiving an item, or its versions and runs, leaves
the list as it was. Under it, filter buttons (Agents: All, Used, i.e. run at least once,
Unused and Archived; Skills: All, Used, i.e. by some live agent's current version, Unused, each
saying "Used by 3 agents", and Archived; Workflows: All, Empty and Archived; Archived lists the
archived items, which the others leave out) and a sort menu (by name, the default, by most recently updated, and for
agents by most runs) narrow and order the list, combined with the search and kept in the URL too
(`?filter=used&sort=runs`); **Clear filters** resets them all. The Agents list also has an
**Any skill** and an **Any tool** select, for the agents whose current version uses that skill
(any version of it) or has that tool (`?skill=12&tool=web_search`). Pressing `/` jumps to the
list's search box, except while typing in a field.

## Versions

Agents and skills are **versioned**. Each has an editable identity row (name, description) and a
chain of immutable versions (`agent_versions`, `skill_versions`) holding everything that affects
the model's behaviour. They're all in the organisation's store, with the skill versions each
agent version pins (`agent_version_skills`) and its tools (`agent_version_tools`); see
[Stores](stores.md#agents-skills-and-workflows):

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
- Skills are archived, never deleted (old agent versions still pin their versions): see
  [Archiving a skill](#archiving-a-skill).
- A skill's page has tabs, like the agent's: Content (when to use it and its instructions), Used
  by (the agents whose current version uses it, with a count) and Versions (with a count). The
  open tab is in the URL (`/skills/12/versions`); a version's page links back to Versions,
  and restoring a version returns there.
- Each run is traced to Langfuse when it's configured (see [Tracing](tracing.md)), with
  `agent_id`, `agent_version`, `agent_version_id`, the skill versions and the tools with their
  settings as trace metadata, and the run's thread id, `agent-run-<id>`, as the Langfuse
  session.
- Agents are archived, never deleted (older workflow versions keep pinning their versions: see
  [Workflows](workflows.md#versions), and their runs are the members' history with them). Names
  are unique among all the organisation's rows, archived ones included, so an archived item's
  name stays reserved and unarchiving it never clashes: creating one with an archived agent's
  name answers 409 "The organisation already has an agent called <name> (archived)."
- Saves and archives lock the rows involved (skills, then agents, then workflows, each by id),
  so an archive can't race a save that starts using what it archives. No route deletes an
  agent, skill or workflow; deleting a single run still works (see
  [Deleting a run](#deleting-a-run)).

**Skills are inlined into the agent's instructions** (base prompt, then each pinned skill's
description and instructions in a `<skill>` block). That's simple and works well for a handful
of skills. With many large skills, switch to progressive disclosure: list only names and
descriptions, and give the agent a tool to load a skill's full instructions.

## Runs

Each conversation with an agent is saved as a **run** (a LangGraph thread), whose page shows the
conversation and an event log: messages, model responses, tool calls and results, the
instructions sent and token usage. A run also records how it went and what it cost: a status
(running, succeeded, failed), when its latest turn began and ended, and, over all its turns, the
model's tool calls and its input and output tokens (each model call's, summed, so the context is
counted again at each step, as bills count it). The run list and the run page show these; runs
from before they were kept show "not tracked" rather than zero, and the run page's event log
still totals the stored messages for them.

- Each run is a LangGraph thread (`agent-run-<id>`) kept by its organisation's store's own
  Postgres checkpointer, beside the run (runs, their sandbox sessions, workflow runs and their
  steps are all in the store: see [Stores](stores.md#runs)), the client sends only its new message, and the agent continues from the thread's latest
  checkpoint. The user's message is checkpointed before the model is called; if a turn fails or
  is stopped, the reply's text so far (or an error result for each tool call that hadn't
  finished) is added to the thread, so the next turn sees what the user saw. Turns of the same
  run are serialized by a **turn lease** on the run's row (`aios.turn_lease`): a second tab's
  turn waits for the first (on the run's release channel in Redis, polling each second as a
  fallback), then builds on it. A turn holds the lease for `TURN_LEASE_SECONDS` (90) at a time,
  refreshed by its heartbeat, so a process that dies frees its runs once that passes; one that
  waits past `TURN_WAIT_SECONDS` (120) answers 409 "This run is busy" on the run page. Every
  checkpoint write first checks, in its own transaction, that its turn's lease still holds
  (`checkpoints.FencedSaver`), so a turn that lost it (frozen past its expiry, or stopped) can't
  write the run's history. No database session state is involved, so it works through the
  connection pooler. A run is named by its store and its id there (ids repeat across stores), so
  are its lease's Redis channels, and its trace session is `agent-run-<organisation>-<id>`.
  `agent_runs` keeps a message count and title for listings, and the run's status, times and
  counters, written by each turn.
- **The checkpointer's tables are LangGraph's, not Alembic's.** Each store has them in its
  `langgraph` schema, made and migrated with LangGraph's own migrations by the store's owner
  (`checkpoints.set_up_store`): by store revision 0010, before the store's first turn, and again
  each time the store is migrated, so a newer LangGraph's reach every store. The checkpointer
  connects to the store as its read-write role, through the pooler, setting nothing at startup
  and preparing no statements: the role finds the schema through its default search path in
  the store (`aios, extensions, langgraph`, set by the provisioner). The store tree's
  autogenerate leaves the schema out. Deleting a run deletes its thread in the same
  transaction (they aren't foreign keys, so this is done explicitly), after locking the run's
  row, which a checkpoint write being made holds until it commits.

Runs start by hand, from a chat mention (see [Chats](chats.md)), from a schedule (see
[Triggers](triggers.md)) or as a workflow's agent node (see [Workflows](workflows.md)).

## The documents assistant

Each new chat has the organisation's **documents assistant** as its first agent
(`aios.assistant`): the glossary's organisation assistant, today an agent of the
organisation's, made for whichever member first creates a chat (`agents.builtin =
'documents'`, one live one per organisation), with the document search tool and instructions
to ground its answers in the results, citing sources. It calls the hybrid search tool (vector
plus full-text, fused with Reciprocal Rank Fusion: see [Tools](tools.md#search)) over the
organisation's documents. It's mentioned and answers like any agent chat member
(acknowledgement, brief, card and reply: see [Chats](chats.md)). It's listed with the
organisation's other agents, where it can be edited like one, or archived (it stays the
organisation's one assistant, and new chats still get it). Chats made before it existed don't get it, but their owner can add it like any
agent.

## Archiving an agent

A member archives an agent from its page in one confirmation, instead of deleting it: nothing is
lost, and nothing new starts on it. Any member may archive and unarchive; phase 1's roles later
give this to editors and admins. Workflows and skills are archived on the same terms
([Archiving a workflow](workflows.md#archiving-a-workflow), [Archiving a skill](#archiving-a-skill)).

- `POST /api/o/{org}/agents/{id}/archive` sets `archived_at` and `archived_by` (who archived it) and
  pauses its schedules. It's never refused, whatever uses it, and archiving an archived agent
  changes nothing. `POST /api/o/{org}/agents/{id}/unarchive` clears both; its schedules stay paused
  until someone resumes them, so unarchiving doesn't set off a burst of runs. Both answer the
  agent, which carries its archived fields everywhere (`archived_at`, `archived_by`).
- `GET /api/o/{org}/agents/{id}/used-by` is what the confirmation shows, read-only
  (`aios.used_by.agent`): the live workflows that run it at any depth (with their schedules),
  its own schedules, the chats it's in (those the member may see) and the data tables it will
  leave read-only, which stays empty until tables have owners.
- An archived agent starts no new turns. Each start path refuses with 409 "The agent <name> is
  archived.": a run or a turn started by hand (`POST .../runs`, `POST .../runs/{id}/chat`), a
  schedule firing (recorded on the schedule as its error), and a workflow run whose pinned graph
  runs it at any depth (`aios.workflows.archived_in_graph`, which also refuses an archived
  nested workflow). A mention posts the message, and its invocation fails at once with "The
  agent <name> is archived", before the handoff, so no model is called.
- Turns and runs already going aren't checked again: a turn underway finishes, and a workflow
  run already past its start runs the agent's node.
- In a chat it stays a member, shown as archived (`archived` on the member), and drops out of
  mention suggestions (`GET /api/o/{org}/chats/{id}/mentionable`). Unarchiving restores both.
- `GET /api/o/{org}/agents` leaves archived agents out; `?archived=true` lists only them, which the
  Agents list's Archived filter shows. The agent's page shows an archived badge and notice, and
  Unarchive in place of New run.

`make seed` rebuilds development data (see [Development](development.md#seed-data)), so nothing
was kept for agents, skills or workflows deleted before archiving existed.

## Archiving a skill

A member archives a skill from its page, in one confirmation that lists, read-only, the agents
whose current version uses it (`GET /api/o/{org}/skills/{id}/used-by`). It's always allowed,
even while agents use it.

- `POST /api/o/{org}/skills/{id}/archive` and `/unarchive` set and clear `archived_at` and
  `archived_by`, and answer the skill. Archiving an archived skill changes nothing.
- Agent versions that pin it keep running with it: their instructions were rendered when they
  were saved.
- It can't be added to a new agent version: a save that would make a new version including it
  (creating an agent with it, or changing an agent that keeps it) answers 409 "The skill <name>
  is archived." A save that makes no new version, like a rename, goes through; so does one that
  removes the skill. Restoring an old version that pins it is refused too.
- An agent whose current version uses it says so: each skill on the agent carries `archived`,
  and the agent's page shows a notice naming them and marks each archived. The edit form keeps
  it attached, marked, until it's removed.
- `GET /api/o/{org}/skills` leaves archived skills out; `?archived=true` lists only them, which
  the Skills list's Archived filter shows. The skill's page shows an archived badge and notice,
  and Unarchive in place of Archive.

## Deleting a run

Deleting a run (`DELETE /api/o/{org}/agents/{id}/runs/{run_id}`) deletes its conversation, its
thread and its sandbox, and cancels its pending schema change requests. A turn still underway on
it is stopped (`checkpoints.stop_turns`): its lease is cleared and a stop signal is published,
which the process holding it acts on by cancelling the turn, so a model or tool call underway
ends at once. If the signal doesn't arrive (Redis down), the turn fails at its next write, on
fencing. No connection is ended: through the pooler that would break another turn's. A turn
that only takes its run's lease after the run has gone finds it gone and does nothing, so no
thread is left behind.

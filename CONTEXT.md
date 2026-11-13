# aios

A platform where teams work with managed agents and workflows that hold all the context the team
needs in a database the organisation owns. These are the words the product and its code use for
what it is about.

## Organisations and people

**Organisation**:
The group that shares one store, owns everything its members make, say and produce, and is the
boundary of who can see what. Usually a whole company; a company that needs departments kept
apart uses one organisation each. Called "Organisation" on screen too.
_Avoid_: Tenant, workspace, account

**Member**:
A person in an organisation. A person may be a member of several organisations.
_Avoid_: User (for someone in an organisation), seat

**Role**:
What a member may do in an organisation: one of admin, editor or participant.

**Participant**:
A member who works with the agents and workflows others have set up: chats, mentions, starts
runs. A participant's runs act with whatever powers the agent was given.
_Avoid_: Viewer, guest, user

**Editor**:
A member who builds, changes, archives and unarchives agents, skills and workflows, and builds
and changes triggers.
_Avoid_: Builder, member (as a role)

**Admin**:
A member who also runs the organisation: members, roles, connectors, settings, archiving chats,
releasing flagged content and destructive approvals.
_Avoid_: Owner, creator

## The store

**Store**:
The organisation's own isolated database, and the system of record for everything the
organisation makes, says and produces. Screens show its contents as "Data".
_Avoid_: Data store (in prose), isolated database, organisation database, knowledge base,
workspace

**Platform database**:
The shared database that knows only who people are, which organisations exist, and how to reach
and schedule work in each store.
_Avoid_: Main database, app database

**Data table**:
A table in the store that an agent asked for through a schema change request, holding the
organisation's structured data.
_Avoid_: User table, custom table

**Table owner**:
The agent that stewards a data table and may write it; other agents may only read it unless
shared. Editors and admins share and transfer tables. A table whose owner is archived is
read-only until ownership is transferred.

**People mirror**:
`aios.people` in the store: a copy of the organisation's members (id, name, role) that the
platform keeps up to date, so agents can answer "who" from the store alone.

**Reporting view**:
A view in the store that summarises runs, turns, usage or the audit log for agents and Insights.

**Deny list**:
The few columns and tables (secrets, credentials, people's emails, audit log details) kept out
of the data tools' grants.

**Retention**:
How long an organisation keeps its runs and its chat history before they're pruned; "keep
indefinitely" is the default.

## Agents, skills and workflows

**Agent**:
A configured model with a base prompt, skills and tools, that runs for the organisation and can
be a member of chats.
_Avoid_: Bot, assistant (except the organisation assistant)

**Organisation assistant**:
The built-in agent every organisation has, which can search and query everything in its store.
_Avoid_: Documents assistant

**Skill**:
Reusable instructions an agent is given.

**Tool**:
A capability from the tool library that an agent version may use, with its settings.

**Workflow**:
A graph of agents and other workflows that runs as one unit.

**Node**:
One agent, workflow or approval step in a workflow's graph.

**Condition**:
A rule on an edge between two nodes (JSONLogic, never code): the edge is taken only when it
holds. A node no taken edge reaches is **skipped**, and so is everything only after it.

**Input template**:
How a node builds its input from named fields of the run's input and its predecessors' outputs:
placeholders only, no expressions.

**Version**:
An immutable snapshot of an agent's, skill's or workflow's configuration; editing creates a new
one. Workflow runs, and runs waiting on an approval, stay on the version they started on; an
agent's ongoing run in a chat takes the current version at each turn.

**Archived**:
Put away rather than deleted, and can be unarchived. An archived agent, skill or workflow
starts no new runs but keeps its history, memory and tables; an archived chat is read-only but
still searchable.
_Avoid_: Deleted

**Memory**:
What an agent has chosen to remember across its runs, private to that agent and visible to
people. It isn't tied to the chat it was learned in. Jev checks it as it's written.

## Running

**Run**:
An agent's working session in one chat or for one trigger, or a workflow's execution. An agent's
run may last one turn (a schedule's) or many (its **conversation run** in a chat: one per agent
and chat, each mention a new turn of it, on the agent's current version); other runs stay on the
version they started on. A run itself has no outcome.

**Turn**:
One exchange within an agent's run: a message in, the agent's work, and its reply, on one
version of the agent. Turns are what is counted, budgeted and judged as succeeded or failed.

**Invocation**:
One request for an agent or workflow to act, whatever caused it (a mention, a schedule, an
event, a person starting it by hand, or another agent), recording who or what caused it and
where its result goes.
_Avoid_: Job, task

**Trigger**:
Something that starts runs without a person asking at the time: a schedule or an event.

**Event**:
Something that happened in the store that triggers can react to, such as a document ingested,
a data table written or a chat message posted.

**Usage**:
The billable work a run causes (model calls, embeddings, searches, sandbox time), recorded
against who and what caused it.

**Cap**:
A configurable limit on one run or turn: model calls, tokens or cost.

**Allowance**:
A configurable monthly spending limit for a role or one member, charged to whoever caused the
usage.

**Budget**:
The organisation's monthly spending limit, set by an admin.

**Invocable by**:
An agent's or workflow's setting for who may start it: everyone, editors or admins.

**Run service**:
The shared service that runs every agent turn and workflow node, for every organisation, from
a fair queue. Each of its worker processes is an **executor**.
_Avoid_: Run executor, executor service, worker (for the service)

**Lane**:
A kind of work in the run service's queue, claimed in its own order: interactive, background,
sources, ingest, and urgent or routine maintenance.

**Turn lease**:
The hold a turn takes on its run so that two turns of one run never overlap.
_Avoid_: Turn lock

## Chats

**Chat**:
A conversation in an organisation, and the only kind there is. Every chat is open: any member
can read it without joining, and posting joins it. Only the agents and workflows it includes
can be mentioned there. A new chat starts with the agents an admin has chosen as defaults.
_Avoid_: Channel, room, conversation, public chat, private chat, direct message, agent chat,
run chat

**Chat member**:
A person, agent or workflow in a chat; always "chat member", never just "member". Every chat
member is equal: a chat has no owner.
_Avoid_: Participant (a role), chat owner

**Mention**:
Naming an agent, workflow or person in a message; mentioning an agent or workflow creates an
invocation.

**Brief**:
The summary of a chat that an agent or workflow is given before it acts on a mention.
_Avoid_: Context, prompt

**Handback**:
The summary of a run's result that is posted back to a chat in place of its raw output: a
mentioned workflow's result, a delivered result, or an agent's reply that's long or JSON. The
full output stays one click away on the run card.

**Draft**:
An agent's reply as it's being written, streamed into the chat and replaced in place by the
reply once it's posted. Never stored.

**Delivery**:
Posting a trigger's run result into a chosen chat once the run ends, as a handback (or a short
failure message), from the agent or workflow.

**Chat with this agent**:
The action on an agent's page that makes a new open chat including only that agent, named for
the agent, the person and the date. Not a private conversation.
_Avoid_: Agent chat, direct message

**Default chat agents**:
The ordered list of agents, chosen by admins, that every new chat includes (the organisation
assistant unless they chose otherwise); not used by **Chat with this agent**.

**Notification**:
An entry in a person's bell, across all their organisations, saying that something needs them or
has happened to what they asked: a run failed, a reply came, they were mentioned, an approval
needs them. It holds references, never content.

## Context coming in

**Source**:
Anything that brings content into the store: uploads, web pages, webhooks, an email inbox or a
connector.

**Connector**:
An organisation's connection to an external system, such as Slack, Google Drive or GitHub, that
can sync its content and give agents its tools.
_Avoid_: Integration, plugin

**Synced item**:
A searchable copy of content whose system of record is an external system; it follows its
source and is removed when the source removes it. Syncing shares it with the whole
organisation, whatever its permissions were at the source.

**Document**:
A file in the store, parsed and indexed for search. An uploaded document belongs to the
organisation and stays until someone deletes it; a synced document is a synced item.

**Jev**:
The external classifier that checks untrusted content for prompt injection before it reaches a
model. aios integrates it; it doesn't build its own.

**Untrusted content**:
Anything that comes from people or from outside: chat messages people send, documents, synced
items, webhook bodies and tool results. It's classified by Jev, and always wrapped in an
**untrusted block** in a model's context, whatever its verdict.

**Derived**:
Text agents write from possibly untrusted content (posts, handbacks, briefs, run messages,
summaries, recalled memory). It isn't classified again, but is always wrapped as untrusted.

**Verdict**:
What Jev's check recorded on a piece of content: `awaiting_check`, `clean`, `flagged`,
`released` or `derived`. Only clean, released and derived content is searchable.

**Flagged list**:
The list in Settings of flagged documents and synced items, where an admin can release one. A
released item is still wrapped as untrusted, and the release is in the audit log.

**Approval**:
Something a member with the right role must decide, approving or rejecting it, before a run
can carry on: a schema change, or a workflow's approval step.
_Avoid_: Request, sign-off

**Schema change**:
The kind of approval in which an agent asks for a new or changed data table.
_Avoid_: Schema change request (in prose), migration

## Relationships

- An **Organisation** has many **Members**, each with one **Role**, and exactly one **Store**.
- An **Agent**, **Skill** or **Workflow** has many **Versions**; each **Turn** runs on one, and a
  **Run** other than a conversation run stays on the one it started on.
- An **Invocation** has one cause; a **Mention**, **Trigger**, person or **Agent** causes it.
  It starts a **Run** or a new **Turn** of one, and its result goes back as a **Handback** or
  the agent's reply.
- A **Run** has many **Turns**; an **Agent** has one ongoing **Run** per **Chat** it's in.
- A **Connector** is one kind of **Source**; what it syncs are **Synced items**.
- An **Agent** may be the **Table owner** of many **Data tables**.

## Flagged ambiguities

- "Member" was both a person in an organisation and one of three roles; the role is now
  **Editor**, and "member" means anyone in the organisation.
- "Data store" and "isolated database" both meant the **Store**.
- "Run" meant both the conversation and one execution of it; the working session is the **Run**,
  an execution is a **Turn**.
- "Invocation" meant only what a mention made; it now covers every cause.
- "Private chat" and "direct message" are gone: every **Chat** is open, and chats no longer
  have owners.
- "Tenant" has been used for both an organisation and the whole aios service; use
  **Organisation**, or name the service.
- "Team" is reserved for the roadmap's groups inside an organisation; it never means the
  organisation itself.
- "Budget" covered every usage limit; per-run limits are **Caps**, per-person limits are
  **Allowances**, and only the organisation's limit is the **Budget**.
- "Executor" meant both the service and one of its processes; the service is the **Run
  service**, and an **executor** is one process.
- "Flagged" means only Jev's verdict; a Slack channel or scope with a problem is "marked
  restricted" or "needs attention".

# aios

A platform where teams work with managed agents and workflows that hold all the context the team
needs in a database the organisation owns. These are the words the product and its code use for
what it is about.

## Organisations and people

**Organisation**:
The group that owns everything its members make, say and produce. Each has its own store.
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
A member who builds and changes agents, skills, workflows and triggers.
_Avoid_: Builder, member (as a role)

**Admin**:
A member who also runs the organisation: members, roles, connectors, settings, archiving and
destructive approvals.
_Avoid_: Owner, creator

## The store

**Store**:
The organisation's own isolated database, and the system of record for everything the
organisation makes, says and produces.
_Avoid_: Data store (in prose), user database, workspace database

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
shared.

**Retention**:
How long an organisation keeps its runs and its chat messages before they're pruned; "keep
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

**Version**:
An immutable snapshot of an agent's, skill's or workflow's configuration; editing creates a new
one, and runs are pinned to the version they started on.

**Archived**:
An agent, skill or workflow that starts no new runs but keeps its history and can be restored.
_Avoid_: Deleted

**Memory**:
What an agent has chosen to remember across its runs, private to that agent and visible to
people.

## Running

**Run**:
One execution of an agent or a workflow, pinned to a version.

**Turn**:
One exchange within an agent's run: a message in, the agent's work, and its reply.

**Trigger**:
Something that starts runs without a person asking at the time: a schedule or an event.

**Event**:
Something that happened in the store that triggers can react to, such as a document ingested,
a data table written or a chat message posted.

**Usage**:
The billable work a run causes (model calls, embeddings, searches, sandbox time), recorded
against who and what caused it.

**Budget**:
A configurable limit on usage, for a run, a member or the organisation.

## Chats

**Chat**:
A conversation between members, agents and workflows in an organisation, public or private.
_Avoid_: Channel, room, conversation

**Direct message**:
A private chat between one person and one agent.
_Avoid_: Agent chat, run chat

**Mention**:
Naming an agent, workflow or person in a message; mentioning an agent or workflow asks it to
act.

**Invocation**:
The request a mention makes of an agent or workflow, from acknowledgement to the result posted
back.

**Brief**:
The summary of a chat that an agent or workflow is given before it acts on a mention.
_Avoid_: Context, prompt

**Handback**:
The summary of a run's result that is posted back to a chat in place of its raw output.

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
source and is removed when the source removes it.

**Document**:
A file in the store, uploaded or synced, parsed and indexed for search.

**Schema change request**:
An agent's request for a new or changed data table, which a member with the right role
approves or rejects.

## Relationships

- An **Organisation** has many **Members**, each with one **Role**, and exactly one **Store**.
- An **Agent**, **Skill** or **Workflow** has many **Versions**; a **Run** is pinned to one.
- A **Chat** has **Members**, **Agents** and **Workflows** in it; a **Mention** of an agent or
  workflow creates an **Invocation**, which starts a **Run** with a **Brief** and ends with a
  **Handback** or the agent's reply.
- A **Connector** is one kind of **Source**; what it syncs are **Synced items**.
- An **Agent** may be the **Table owner** of many **Data tables**.

## Flagged ambiguities

- "Member" was both a person in an organisation and one of three roles; the role is now
  **Editor**, and "member" means anyone in the organisation.
- "Data store" and "isolated database" both meant the **Store**.

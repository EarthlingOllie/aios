# Phase 3: Agents and automation

Covers plan steps 3.1–3.7. Decisions: decision record 0001 (D3, D5, D7, D12, D13). Plan: the
platform cohesion plan. Words: the glossary (CONTEXT.md). Deferred work: the roadmap.

- **3.1** Continuing runs
- **3.2** Memory
- **3.3** Table ownership and per-agent database roles
- **3.4** Structured workflows and approvals
- **3.5** Budgets
- **3.6** Event triggers
- **3.7** Lanes and autoscaling

## Problem Statement

After phases 1 and 2, agents live in the organisation's store and answer in streamed chats. They
still don't behave like team members, and the organisation can't automate around them safely.

### Members and participants

- **Agents forget.**
  - Every mention of an agent in a chat starts a brand new run, briefed on the last thirty or so
    messages. Ask the Researcher something in #launch on Monday, and on Tuesday it has no
    thread to pick up.
  - Nothing it learns carries over to the next run.
  - The only cross-run memory there was, the run history export, was removed in phase 1. The
    unified search over past runs isn't the same as an agent knowing something.

### Editors

- **Fixes don't reach existing conversations.** Each run is pinned to the version it started on.
  When an editor fixes a bad instruction, conversations already under way keep making the same
  mistake.
- **Workflows are thin.**
  - Nodes pass plain text joined by blank lines, so one node can't hand another a score or a
    list.
  - There's no way to branch ("escalate if the score is high, otherwise archive").
  - The only pause for a person is a schema change. A workflow can't stop and ask an admin to
    sign off a spend.
  - An agent can't start a workflow.
- **Automation can only start on a timer.** An editor can't say "when a new document lands,
  summarise it", "when the leads table changes, enrich it", "when someone writes 'outage' in
  #support, open a ticket", or "when our billing system calls us, reconcile".

### Admins

- **Any agent can write any table.** An agent that should only read the Researcher's
  `competitors` table can overwrite it, because every agent with the write tool writes as the
  same database role. The application doesn't check, and nor does the database. Nobody can say
  which agent owns which table.
- **There is no ceiling on spend.** Phase 1 added per-turn caps and mention limits, but nothing
  stops a month's usage running away. A participant who mentions agents all day, or an event
  trigger that fires on every message, has no bound.
- **Schema changes and sign-offs are two systems.** The schema change list on the Data page will
  sit beside a second kind of decision once workflows can pause, unless the two are one.

### Operators and developers

- **Every kind of work queues together.** After phase 1, every agent turn runs on a fair
  executor pool. But a burst of background work (200 schedules at 09:00, a flood of events) still
  competes with a person waiting for a reply.
- **Maintenance is one lane.** Urgent maintenance (applying an approved change someone is
  waiting on) shares a single worker with routine sweeps.
- **Nothing scales with load.**
- **Automation makes loops possible.** Agents start workflows, triggers fire on writes agents
  make, and agents write again. Without a rule, two triggers can feed each other forever.

## Solution

### Agents as persistent members

- **One run per conversation.** An agent keeps one run per chat. Each mention is a new turn of
  it, briefed on what has happened since its last turn.
- **Fixes reach conversations.** Each turn uses the agent's current version, and records which
  one, so a fix reaches every conversation at the next turn. Workflow runs, and runs waiting on a
  decision, stay on their version.
- **Compaction.** As a conversation grows past what the model can read, older turns are
  summarised in what the model sees. People and search still see every turn.

### Memory

- Agents remember facts across runs, and recall them through the same search everything else
  uses.
- Memories aren't tied to a conversation: what an agent learns in one chat it can recall in any.
- People see, edit and delete an agent's memories on its page.

### Table ownership

- **Owners.** The agent whose schema change created a table owns it. Only the owner, and agents
  it's shared with, may write it; every agent may still read it.
- **Managing access.** Editors and admins share, unshare and transfer tables on the Data page.
- **Archived owners.** A table whose owner is archived is read-only until it's transferred.
- **Enforced by the database.** Each agent that writes has its own database login, granted
  exactly its tables. SQL written by a model always runs as a role that can't write at all.

### Structured workflows

- **Structured data.** Workflows and their nodes can declare JSON Schemas for what they take and
  give. Agent nodes with an output schema return validated structured output, and nodes can build
  their input from named fields of earlier nodes.
- **Branches.** Edges can carry simple conditions. Nodes no path reaches are skipped, visibly.
- **Approval nodes.** A new kind of node pauses the run for a person in a given role to approve
  or reject, and the run can branch on the answer.
- **One approvals list.** Schema changes and approval steps are both **approvals**: one list, one
  set of notifications, expiry and resume behaviour.
- **Agents can start workflows.** They get the result back, or have it posted to the
  conversation later if it takes a while.

### Budgets

- Admins set a monthly budget for the organisation, and optional allowances per role or person.
- Admins are warned at 80%.
- At 100%, new work is refused before it costs anything, and work already going finishes.

### Event triggers

- Triggers can start agents and workflows when something happens in the store:
  - a document is ingested;
  - a data table is written;
  - a schema change is applied;
  - a message is posted in a chat;
  - an external system calls a signed webhook.
- Filters narrow what matches, and bursts can be gathered into one run.
- **Loops are cut.** A trigger never fires on its own target's events unless asked, never fires
  twice in one chain of causes, and chains have a maximum depth.

### Capacity

- Interactive work (someone waiting in a chat) has reserved capacity and goes first.
- Urgent maintenance has its own lane.
- Workers scale up with queued work and back down when idle.

## User Stories

### Continuing runs (3.1)

1. As a participant, I want an agent I mention in a chat to continue the same conversation it
   had there yesterday, so that I don't have to re-explain what we were doing.
2. As a participant, I want an agent's reply to take account of everything said since it last
   spoke, so that it doesn't miss what others added in between.
3. As an editor, I want a fix I save to an agent to apply at its next turn in every chat it's
   in, so that a bad instruction stops causing harm straight away.
4. As an editor, I want each turn to record which version of the agent produced it, so that I
   can tell whether a bad answer came before or after my fix.
5. As an editor, I want a workflow run to keep using the agent version it was started with, so
   that a run's behaviour doesn't change halfway through.
6. As a member, I want an agent waiting on an approval to finish that turn on the version it
   started with, so that the decision is applied to the request as it was made.
7. As a participant, I want my mention of an agent that is waiting on an approval to be queued
   with a clear "Waiting for an approval" status, so that I know why it isn't answering yet.
8. As a participant, I want queued mentions to be answered in the order they were made, so that
   the conversation stays coherent.
9. As a participant, I want queued mentions to run even if the approval they waited on is
   cancelled, so that my questions aren't lost.
10. As a participant, I want a second mention made while the agent is still replying to wait for
    that reply, so that two turns never talk over each other.
11. As an editor, I want a turn to fail with a clear reason when the agent's current version
    can't run (a tool that left the library, say), so that I know to fix the agent rather than
    getting answers from an older version.
12. As a participant, I want an agent in two different chats to keep two separate conversations,
    so that #sales doesn't get answers shaped by #launch.
13. As a participant, I want a very long conversation with an agent to keep working, so that a
    months-old chat doesn't break when it outgrows the model's context.
14. As a member, I want the full history of a conversation to stay visible and searchable after
    the agent's view of it is summarised, so that nothing anyone said is lost.
15. As an admin, I want summarising a conversation to be recorded as usage, so that its cost
    shows in Insights and counts against budgets.
16. As an admin, I want retention pruning to remove old turns of a long conversation turn by
    turn, so that a conversation in daily use is never pruned whole.
17. As a participant, I want an archived agent to refuse mentions with a clear reason, so that I
    don't wait on an agent that won't answer.
18. As an editor, I want a schedule or event that delivers an agent's result to a chat to post
    it there without joining the agent's conversation, so that automated output doesn't change
    how the agent talks to people in that chat.
19. As a member, I want the run page to list each turn with its version, status and usage, so
    that I can see how a conversation went over time.
20. As a member, I want the run page to mark where earlier turns were summarised, so that I can
    tell what the agent could still see verbatim.
21. As a developer, I want summaries never to separate a tool call from its result, so that the
    model provider never rejects the history.

### Memory (3.2)

22. As an agent, I want to store a fact I'll need later, so that I can use it in future runs.
23. As an agent, I want to forget a fact that's no longer true, so that I don't act on stale
    information.
24. As an agent, I want to recall my memories by searching for them, so that I find what's
    relevant without being handed everything.
25. As an agent, I want storing the same fact twice to refresh it rather than duplicate it, so
    that my memory stays clean.
26. As an agent, I want to be told when I'm at my memory limit, so that I can forget something
    before I remember more.
27. As a member, I want memories an agent learns in any chat, or in runs with no conversation,
    to be available wherever the agent works, so that shared knowledge is actually shared.
28. As an editor, I want to see an agent's memories on a Memory tab, with where each was learned,
    so that I understand why it behaves as it does.
29. As an editor, I want to edit or delete an agent's memories, so that I can correct what it got
    wrong.
30. As an admin, I want edits and deletions of memories recorded in the audit log, so that
    changes are accountable.
31. As an editor, I want an agent's memories kept when it's archived and back when it's
    unarchived, so that archiving doesn't wipe what it knows.
32. As an admin, I want memories never pruned by retention, so that agents don't lose knowledge
    because old chats were pruned.
33. As an editor, I want memories left out of agent exports, so that what an agent learned in
    one organisation never travels to another.
34. As an editor, I want an agent version without the remember tool to still recall its existing
    memories, so that removing the tool stops new memories without losing old ones.

### Table ownership and per-agent roles (3.3)

35. As an editor, I want the agent whose schema change created a table to own it automatically,
    so that every new table has someone responsible for it.
36. As an editor, I want only a table's owner and the agents it's shared with to write it, so
    that one agent can't overwrite another's data.
37. As an agent, I want to read every table, so that I can use the organisation's data while
    only writing my own.
38. As an agent, I want a refused write to tell me who owns the table, so that I can explain to
    people what's needed.
39. As an editor, I want to share write access to a table with another agent, so that two agents
    can maintain it together.
40. As an editor, I want to unshare a table, so that an agent that shouldn't write it any more
    can't.
41. As an editor, I want to transfer a table to another agent, so that ownership follows whoever
    now maintains it.
42. As an admin, I want approval rules to be able to require an admin for ownership changes, so
    that sensitive tables can't be handed over casually.
43. As an admin, I want every share, unshare and transfer recorded in the audit log, so that I
    can see who gave which agent access.
44. As an editor, I want a table whose owner is archived to become read-only, including for the
    agents it was shared with, so that nothing writes it without a responsible owner.
45. As an editor, I want the archive dialog to list the tables that will become read-only, so
    that I know what archiving affects.
46. As an editor, I want the Data page to show each table's owner, who it's shared with, and
    when its owner is archived, so that access is visible at a glance.
47. As an editor, I want access changes to show as pending until the database has applied them,
    so that I know when they take effect.
48. As an editor, I want tables with no owner to stay writable by any agent, and marked "No
    owner", so that agents keep working until someone assigns one.
49. As an editor, I want to assign an owner to an unowned table, so that I can tighten access
    when I'm ready.
50. As an editor, I want a dropped and restored table to keep its owner and shares, so that
    restoring doesn't reset access.
51. As an admin, I want the database itself to refuse a write by an agent that doesn't own or
    share the table, so that a bug in the application can't bypass ownership.
52. As an admin, I want SQL written by a model to run only as a role that can't write anything,
    so that no query an agent writes can change data.
53. As an admin, I want an agent's database login unable to switch to any other role, so that
    an agent can't borrow another's access.
54. As an admin, I want an agent's database login unable to reach any other organisation's store
    or the platform database, so that isolation between organisations holds.
55. As an agent, I want a write to a table whose access is still being set up to tell me to try
    again shortly, so that a timing gap doesn't fail my turn.
56. As an operator, I want agents' database logins to go through the pooler with their own
    connection limits, so that many agents don't exhaust a store's connections.
57. As an operator, I want each agent's database password generated, encrypted at rest, and sent
    to Postgres only as a verifier, so that credentials never appear in logs.
58. As an operator, I want archiving an agent to close its connections and revoke its grants, so
    that an archived agent holds nothing open.
59. As a member, I want an agent's own database role able to read chats, briefs, run messages
    and memory but never write them, so that conversation history changes only through aios.
60. As a developer, I want an adversarial test suite that tries every known way to escape an
    agent's database role, required to pass before merging, so that a change to grants can never
    silently open a hole.

### Structured workflows (3.4)

61. As an editor, I want to declare a JSON Schema for a workflow's input, so that every run
    starts with the data it expects.
62. As an editor, I want a run with input that doesn't fit the schema to be refused with the
    validation errors, so that bad input never starts a run.
63. As an editor, I want the start-run form to be generated from the input schema, so that
    people fill in fields rather than writing JSON.
64. As an editor, I want to declare an output schema for an agent node, so that it returns
    structured data the next node can use.
65. As an editor, I want an agent node's structured output validated, with one retry that shows
    the model what was wrong, so that occasional slips don't fail the run.
66. As an editor, I want a node whose structured output still doesn't validate to fail with the
    path and message, so that I can fix the prompt or schema.
67. As an editor, I want to build a node's input from named fields of its predecessors and the
    run input, so that each node gets exactly what it needs.
68. As an editor, I want saving to refuse a template that refers to a node that isn't a
    predecessor, so that mistakes are caught before a run.
69. As an editor, I want a run to fail, naming the missing field, when a template refers to a
    field a predecessor didn't produce, so that I know which node to fix.
70. As an editor, I want nodes without templates to get their input as today, so that existing
    workflows keep working unchanged.
71. As an editor, I want to declare a workflow output schema that the final output is checked
    against, so that whatever consumes the result can rely on its shape.
72. As an editor, I want a workflow with several final nodes to output an object keyed by node,
    so that every final result is kept.
73. As an editor, I want to put a condition on an edge, so that the workflow branches on a
    node's output.
74. As an editor, I want conditions to be simple data, not code, so that nothing anyone writes
    is ever executed.
75. As an editor, I want saving to check that the fields a condition reads exist in the source
    node's schema, so that typos are caught early.
76. As an editor, I want a simple builder for comparing one field with a value, with a raw editor
    for anything more, so that common branches are easy to set up.
77. As an editor, I want a node that no active path reaches to be skipped, and everything only
    downstream of it skipped too, so that branches don't leave nodes hanging.
78. As an editor, I want a node fed by several branches to run when any of them reaches it, with
    only those branches' outputs, so that branches can rejoin.
79. As an editor, I want a run in which no final node ran to succeed with "No final step ran",
    so that an all-skipped path is visible rather than an error.
80. As an editor, I want a condition that can't be evaluated to fail the run, naming the edge,
    so that I can fix it.
81. As an editor, I want workflows without conditions to behave exactly as before, so that
    adding the feature doesn't change existing automations.
82. As a member, I want skipped steps shown greyed on the run page, with structured outputs
    formatted, so that I can follow which path a run took.
83. As a developer, I want a resumed run to rebuild its skips and condition results from its
    recorded steps, so that resuming never re-takes a different path.

### Approvals and the approval node (3.4)

84. As an editor, I want an approval node that pauses a workflow until a person decides, so that
    risky steps get a human sign-off.
85. As an editor, I want to set which role decides an approval node, so that spending sign-offs
    go to admins and routine checks to editors.
86. As an editor, I want the approval node's prompt to be built from its input, so that the
    approver sees exactly what they're being asked to approve.
87. As an editor, I want to branch on the approval's outcome, so that a rejection can notify the
    requester and an approval can carry on.
88. As an editor, I want an undecided approval to expire as a rejection after a set time, so that
    runs don't wait forever.
89. As an editor, I want to set an approval node's own expiry, so that urgent sign-offs time out
    sooner.
90. As an admin, I want to be notified only of approvals I'm allowed to decide, so that my
    notifications are actionable.
91. As an admin, I want schema changes and approval steps in one Approvals list, filterable by
    kind and status, so that I have one place to make decisions.
92. As an approver, I want to see the full reason and input of an approval, so that I can
    decide well.
93. As an approver, I want decide buttons only on approvals I may decide, so that I'm not
    offered actions that will be refused.
94. As an approver, I want dropping data still to need the table names typed, so that
    destructive approvals stay deliberate.
95. As an admin, I want every decision recorded in the audit log, so that sign-offs are
    accountable.
96. As a member, I want cancelling a workflow run to cancel its pending approvals and clear
    their notifications, so that nobody is asked to decide something no longer needed.
97. As a developer, I want schema changes and approval steps to share the waiting, expiry,
    notification and resume code, so that a fix to one fixes both.

### Starting workflows from agents (3.4)

98. As an editor, I want to give an agent a tool that starts specific workflows, so that an
    agent can hand a structured task to an automation.
99. As an agent, I want the tool's description to list the workflows I may start, with their
    input schemas, so that I call them correctly.
100. As an agent, I want a quick workflow's output returned straight to me, so that I can use it
     in the same turn.
101. As a participant, I want a slow workflow's result posted to our conversation when it ends,
     so that I get the answer without the agent waiting.
102. As an editor, I want a slow workflow started from a run with no conversation to deliver its
     result to the trigger's chat, or else only to its run page, so that results always land
     somewhere sensible.
103. As an agent, I want a refusal (wrong input, budget, invocable by, an archived workflow)
     returned as the tool's result, so that I can explain it rather than failing.
104. As an admin, I want an agent starting a workflow that's already running in its chain of
     causes to be refused, so that agents can't loop through workflows.
105. As an admin, I want chains of causes to have a maximum depth, so that indirect loops are
     cut too.
106. As an editor, I want an archived workflow in an agent's allowed list to refuse only that
     call, so that the agent stays usable for its other workflows.

### Budgets (3.5)

107. As an admin, I want to set a monthly budget for the organisation, so that spending has a
     ceiling.
108. As an admin, I want to be warned once when spending reaches 80% of the budget, so that I can
     act before work is refused.
109. As an admin, I want to be told when the budget is reached, so that I know why work is being
     refused.
110. As an admin, I want new work refused at 100% before any model call, so that the refusal
     itself costs nothing.
111. As a participant, I want work already under way to finish when the budget is reached, so
     that nothing is cut off mid-reply.
112. As a member, I want a refused mention or trigger to say the budget was reached, so that I
     understand what happened.
113. As an admin, I want to set default monthly allowances per role, so that participants can't
     spend as much as builders.
114. As an admin, I want to override a person's allowance, so that heavy users can be given more.
115. As an admin, I want allowances charged to whoever started the chain of causes, including
     through triggers fired by their messages and workflows their mentions started, so that
     indirect spending is counted.
116. As an admin, I want schedules and events with no person behind them to count only against
     the organisation budget, so that automation isn't charged to whoever set it up.
117. As an admin, I want this month's spend shown against the budget and each person's
     allowance, so that I can see where money goes.
118. As an admin, I want budget changes to take effect at once and be audited, so that raising a
     limit unblocks work straight away and accountably.
119. As an operator, I want budget checks to read running totals rather than summing the ledger,
     with a nightly reconciliation, so that checks stay fast and correct.
120. As an admin, I want everything to work as before when no budget is set, so that budgets are
     opt-in.

### Event triggers (3.6)

121. As an editor, I want a trigger that fires when a document has been ingested, so that new
     documents are summarised or filed automatically.
122. As an editor, I want to filter document events by source and file type, so that only the
     documents I care about start runs.
123. As an editor, I want a trigger that fires when a data table is written, filtered by table
     and operation, so that changes to data drive follow-up work.
124. As an editor, I want a trigger that fires when a schema change is applied, so that a new
     table can be populated automatically.
125. As an editor, I want a trigger that fires on messages in a chat, filtered by author kind,
     mentions and keywords, so that automations can watch a conversation.
126. As an editor, I want keyword filters to match whole words, ignoring case, so that "outage"
     doesn't match "outages".
127. As an editor, I want a webhook trigger with its own URL and secret, so that an external
     system can start a workflow.
128. As an editor, I want the webhook secret shown once and rotatable, so that a leaked secret
     can be replaced.
129. As an editor, I want a webhook payload checked against the workflow's input schema, so that
     malformed calls never start a run.
130. As an operator, I want webhooks to reject bad signatures, stale timestamps, oversized or
     non-JSON bodies, and repeated delivery ids, so that the public endpoint can't be abused or
     replayed.
131. As an operator, I want the webhook endpoint rate limited per trigger, so that one caller
     can't flood an organisation.
132. As an editor, I want to gather a burst of matching events into one run after a set window,
     so that 30 writes in a minute start one run, not 30.
133. As an editor, I want a batch capped in size, with a count of what was left out, so that a
     flood doesn't build an enormous input.
134. As an editor, I want an event-started agent run to be its own run on the agent's current
     version, so that it doesn't interfere with the agent's conversations.
135. As an editor, I want an event-started workflow's input built from a template over the event,
     or the event itself when there's no template, so that runs get the data they need.
136. As an admin, I want a trigger never to fire on events its own target caused, unless the
     editor explicitly allows it, so that an agent writing a table doesn't re-trigger itself.
137. As an admin, I want a trigger never to fire twice in one chain of causes, so that two
     triggers can't feed each other forever.
138. As an admin, I want chains of causes cut at a maximum depth, shared with workflows started
     by agents, so that long indirect loops stop too.
139. As an admin, I want each trigger rate-limited per minute as a last guard, showing "Rate
     limited" when it's hit, so that a runaway trigger is visible and bounded.
140. As an editor, I want every refusal (loop, depth, rate, budget) recorded on the trigger and in
     the audit log, so that I can see why it didn't fire.
141. As an editor, I want a chat-message trigger to listen to any chat, and deliver to any
     chat, whether or not I'm in it, so that automation can watch the whole organisation.
142. As an editor, I want triggers only creatable by editors and admins, so that participants
     can't set up automation.
143. As a member, I want mentions in what agents and workflows post to still start nothing, so
     that automated posts can't cascade through mentions.
144. As an operator, I want events written in the same transaction as the change that caused
     them, so that no event is lost and none is invented by a rolled-back change.
145. As an operator, I want the dispatcher woken when a store has events, with a sweep as a
     fallback, so that events are handled promptly and never stranded.
146. As an operator, I want dispatched events pruned after a set time, so that the outbox doesn't
     grow forever.
147. As a developer, I want a "drain the events once" entry point for the dispatcher, so that
     tests can drive dispatch deterministically.

### Lanes and autoscaling (3.7)

148. As a participant, I want my mention answered promptly even while hundreds of schedules are
     running, so that background work never makes chat feel slow.
149. As an operator, I want each executor to reserve slots for interactive work, so that a
     background flood can't take every slot.
150. As an operator, I want fairness between organisations kept within each lane, so that one
     organisation can't starve another in either lane.
151. As an admin, I want an approved change applied on an urgent lane that never waits behind
     routine migrations or sweeps, so that people waiting on a decision aren't held up.
152. As an operator, I want routine maintenance on its own lane and workers, so that sweeps,
     measurement and retention never compete with urgent work.
153. As an operator, I want only the maintenance workers to hold the provisioner's credentials,
     so that those powers stay confined.
154. As an operator, I want queue depth and the age of the oldest ready item published per lane,
     so that I can see load and set scaling.
155. As an operator, I want executors to scale on interactive latency and background depth, and
     ingest workers on ingest depth, so that capacity follows demand.
156. As an operator, I want workers to scale back to a small floor when idle, so that we don't
     pay for capacity nobody uses.
157. As an operator, I want scale-in to rely on graceful shutdown, so that shrinking capacity
     never fails a turn.
158. As an operator, I want each service's minimum and maximum configurable, so that I can tune
     cost and headroom.

## Implementation Decisions

Throughout, "store" tables live in each organisation's store (in its platform-owned schema),
which is where phase 1 moved runs, workflows, triggers, chats and approvals. "Platform" tables
live in the shared platform database. Each schema change ships in one change with the code that
uses it, removing what it replaces; development data is rebuilt with `make seed`. Every limit is
a setting with a generous default, overridable per organisation where marked.

### What exists today (verified)

**Agent runs**
- An agent run is "one conversation with an agent", with a LangGraph thread, pinned to the
  version it started on.
- Its status and times describe the latest turn, and its counters sum all turns. There's no
  per-turn record.
- A chat mention always starts a new run of one turn.
- The handoff reads a bounded window of the chat (30 messages or about 8,000 tokens).
- Turns are built with LangChain's agent factory. It sets no middleware and no structured output.

**Data roles**
- Each store has an owner role (no login) plus read-write and read-only login roles, all named
  from the store's id.
- Default privileges let the read-write role write every data table.
- The write tool takes no SQL and writes as the read-write role.
- The query tool runs one model-written statement as the read-only role, in a read-only
  transaction that's always rolled back and reset.
- The drop and restore code revokes and re-grants the two login roles' access per table, column
  by column.

**Workflows**
- Nodes are agent or workflow nodes, and edges have no condition. Inputs and outputs are text,
  joined with a blank line.
- A node runs once all predecessors succeed, and the first failure stops the run.
- A schema change request pauses the node and then the run. A decision re-queues the run, which
  rebuilds its state from its recorded steps.

**Schema change requests**
- They carry a spec, compiled SQL, a reason, the asking run and tool call, a status with eight
  values, the decider, a note and a resume time.
- They're decided through approve and reject endpoints.
- Every member of the organisation counts as an approver until phase 1 adds roles.

**Triggers**
- They have one kind, schedule. Kind-specific settings are columns, and the latest outcome is
  reported on the trigger.

**Celery queues**
- default (ingest and history export);
- workflows (workflow runs and agent resumes);
- provisioning (provisioning, deletion, measuring, applying changes, restores, purges and
  expiry, with the beat).

### 3.1 Continuing runs

- **Conversation runs.** Agent runs gain a reference to the chat they belong to, null for runs
  with no conversation. A partial unique constraint allows one run per agent
  and conversation.
  - **Mentions.** The mention path (the chat invocation code from phase 2) finds or creates
    that run and adds a turn, instead of creating a run.
  - **Other runs.** Runs started by hand, by triggers, by the start-workflow tool, or as
    workflow nodes never set it. A trigger delivering to a chat posts by handback and doesn't
    join the conversation run.
- **Turns.** A turn record holds:
  - its run and invocation;
  - the version it ran on;
  - its status, times, tool calls, tokens and error.

  If phase 1's usage work already introduced a turn record, this step adds the version to it;
  otherwise it adds the record. The run's version reference becomes "the latest turn's version".
- **Version selection** happens at the start of each turn:
  - **Normally:** the agent's current version.
  - **Paused runs:** a run with a pending approval keeps its version until that turn is
    finished.
  - **Workflow nodes:** keep the pinned version.
  - **A current version that can't run:** the turn fails with the reason, and the run keeps its
    last version.
- **The brief.** The handoff module takes an optional "since" message, the one that started the
  agent's previous turn. The window rules still bound it.
- **Queueing.**
  - Mentions arriving while a turn is underway, or while the run waits on an approval, are
    queued invocations, shown with a waiting status.
  - They're claimed in message order after the run is free. A cancelled approval frees the run.
  - The turn lock from phase 1 serialises turns.
- **Compaction.**
  - A model-call hook in the agent builder applies the run's latest summary in place of the
    turns it covers, before each model call. It's LangChain's agent middleware or an
    equivalent; the step plan chooses.
  - When the model's input passes the threshold, the oldest whole turns beyond the kept number
    are folded into a new summary by the handoff model, together with the previous summary.
  - Summaries are stored per run in the store, each recording the last turn it covers.
  - The cut is only ever made between turns, so tool calls and results stay paired.
  - Each compaction is a usage ledger entry with cause "compaction", traced and tagged as such.
  - The checkpointed messages are never altered.
- **Retention** (phase 1) prunes conversation runs turn by turn. A summary is deleted once
  every turn it covers is gone.
- **Settings:**
  - compaction threshold: 120,000 tokens;
  - turns kept verbatim: 6;
  - summary size: 4,000 tokens.

  Compaction ships effectively off (a very high threshold) for one release.

### 3.2 Memory

- **Storage.** A store table of memories, beside phase 1's other conversation tables. Each
  records:
  - its agent and text;
  - its embedding;
  - the run and turn that wrote it, and so where it was learned;
  - when it was created and updated, and who last edited it.

  Memories are indexed in phase 1's unified search index as kind "memory", with the agent.
- **New tools.** Their keys are permanent.
  - **remember(content):** stores a memory. Text equal after normalising case and whitespace
    bumps the existing memory instead. It refuses at the per-agent cap, or over the length
    limit.
  - **forget(memory id):** deletes one of the agent's own memories.
  - **Recall** uses phase 1's search tool with kind "memory", restricted to the calling agent.
    A memory isn't tied to the conversation it was learned in.
- **Memory API.**
  - Lists an agent's memories with where each was learned.
  - Editors and admins may edit and delete them.
  - Edits and deletes are audited.
- **Lifecycle.** Kept through archive and unarchive. Never pruned. Excluded from export.
- **Settings:** memory length, 2,000 characters; memories per agent, 5,000 (per organisation).

### 3.3 Table ownership and per-agent database roles

- **Ownership.** Two store tables:
  - table ownership: each data table's owning agent, or none, with who set it and when;
  - table shares: table, agent, who granted it and when.

  Ownership rows follow tables through drop and restore, and are deleted on purge.
- **Setting ownership.** Applying an approved schema change that creates a table records the
  requesting agent as owner, in the same task as the change. The agent is the one behind the
  approval's run.
- **Tables without an owner.** A table made before this step, or whose owner is cleared, is
  unowned. `make seed` rebuilds development data with owned tables.
- **Who may write:**
  - the owner, and the agents it's shared with;
  - for unowned tables, any agent with the write tool;
  - for a table whose owner is archived, nobody until it's transferred.

  Reads are unchanged: every agent reads every table.
- **Roles and grants**, per store:
  - **A group role for unowned tables:** no login. It's granted write on unowned tables.
  - **One login role per agent that may write anything**, named from the store and the agent,
    within Postgres's identifier limit:
    - no inheritance from anything but the group, and no membership that allows switching
      role;
    - membership of the group with inheritance but without the right to switch to it (Postgres
      16 membership options, which the role setup already requires);
    - a connection limit, and the store's statement and idle timeouts;
    - connect only on its own store;
    - read and write grants on exactly its owned and shared tables, column by column where
      columns are dropped;
    - read on the embedding-column markers the write tool needs.
    - read on conversation content (chats, briefs, run messages, checkpoints, memory), which
      phase 1 keeps in ordinary store schemas that every data tool role can read;
    - **nothing else.** An agent's role writes only the data tables it owns or is shared, the
      group role's unowned tables, and platform tables explicitly allowed for the write tool.
      It never writes conversation content or memory, its own agent's memory included.

      Allowing a platform table to agent roles is an explicit list in the grant derivation,
      never a schema-wide grant or default privilege.
  - **Grant derivation.** The existing per-table grant derivation in the drop code is
    generalised, so drops, restores, purges, applies, shares, transfers and archiving all
    re-derive grants from ownership and shares.
- **What runs as what.**
  - The write tool takes no SQL, and runs as the agent's role.
  - The query tool (the only tool running model-written SQL) stays on the store's read-only
    role.

  This narrows 0001 D5's "its data tools run as that role" deliberately. Reads are open to every
  agent anyway, and model-written SQL then never runs as a role with any write privilege.
  Per-agent read roles wait for teams (roadmap).
- **Credentials.** A platform table of agent store roles holds:
  - the store and agent;
  - the role name;
  - the password, encrypted like store passwords;
  - when the role was created and dropped.

  Passwords reach Postgres only as SCRAM verifiers.
- **Applying grants.** Creating roles and changing grants needs the provisioner, so it runs as
  a maintenance task:
  - on apply;
  - on share, unshare and transfer;
  - on archive and unarchive.

  Changes are reported pending until done. A write that finds its role missing gets a "being set
  up, try again" result without failing the turn. A refused write names the owner.
- **Connections.** Store connections are cached per store and agent, through the pooler, and
  evicted when idle. Archiving disposes of the agent's connections and revokes its grants.
  Unarchiving re-derives the grants with the same role.
- **The read-write role.** Once writes use agent roles, the store's read-write role loses write
  on data tables, and the default privilege that gave it. It keeps writing the platform's own
  store tables (documents, chunks and so on).
- **Ownership API.**
  - The tables listing gains each table's owner (or none, or archived) and its shares.
  - New endpoints set the owner (or clear it), add a share and remove a share. Each answers with
    the table's access and a pending flag.
  - Errors: 403 (role or approval rule), 404, 409 (an archived agent, a duplicate share, or
    sharing with the owner).
- **Settings:** an agent role's connection limit, 10.

### 3.4 Structured workflows and approvals

**Schema changes**
- **Versions.** Workflow versions gain an optional input schema and output schema (JSON Schema
  draft 2020-12, with a size cap). Schemas are part of the configuration hash.
- **Nodes** gain an optional input schema, output schema and input template. Approval nodes also
  have a prompt template, the approver role (editor or admin, default editor) and an optional
  expiry in days. The node kind gains "approval". The target constraint allows approval nodes to
  pin nothing.
- **Edges** gain an optional condition.
- **Runs** gain JSON input and output beside the text ones. Steps gain JSON output, a "skipped"
  status, and a reference to the approval they wait on, which replaces the schema change request
  reference.

**Contracts**
- **Structured output.**
  - An agent node with an output schema is run with the agent factory's structured response
    format.
  - The result is validated with a pinned JSON Schema validator. One failed validation is fed
    back to the model in the same turn. A second fails the step with the path and message.
- **Templates.**
  - Placeholders only: the run input, a field of it, a predecessor's whole output, or a field of
    it. There are no expressions.
  - Saving rejects references to non-predecessors.
  - A missing field at run time fails the step, naming the path.
- **Default input** (no template) is today's rule over the active incoming edges only, with
  structured outputs serialised as compact JSON.
- **Run input** is validated at start for every way of starting a run. A refused start returns
  the validation errors. A mention of a workflow with an input schema passes the brief and
  message as an object, if the schema accepts that shape, and is otherwise refused with that
  reason.
- **Run output.**
  - Exactly one final node ran: its output.
  - Several ran: an object keyed by node.
  - None ran: success with no output, labelled "No final step ran".

  It's validated against the output schema when one exists, and a mismatch fails the run.
- **Conditions** are JSONLogic, evaluated by a small in-house evaluator in the workflows module
  rather than a third-party package. It's a fixed subset:
  - `var` (with a default);
  - equality and inequality, strict and loose;
  - ordering comparisons;
  - negation and double negation;
  - and, or and if;
  - `in`;
  - `missing` and `missing_some`.

  The evaluator reads `output` (the source's output) and `input` (the run input). Saving checks:
  - expressions parse;
  - every operator is allowed;
  - the size is within its cap;
  - every `var` path exists in the declared schemas.

  A source with no output schema allows only its whole output to be read.
- **Readiness.**
  - An edge is active when its source succeeded and its condition, if any, is truthy.
  - A node is ready when every predecessor has finished (succeeded or skipped). It runs if any
    incoming edge is active, and is otherwise recorded skipped, with no agent run; skips
    propagate.
  - Start nodes always run.
  - A condition that errors fails the run, naming the edge.
  - Failure semantics are otherwise unchanged.

  The coordinator step from phase 1's node-by-node execution applies all of this, and the resume
  path rebuilds skips and condition results from recorded steps.

**Approvals**
- **One table.** Today's schema change request table gains:
  - a kind (default schema change);
  - the approver role;
  - an expiry time;
  - a reference to the workflow step that's waiting.

  The schema-change-only fields become optional, and the statuses are extended. It's then renamed
  to approvals in one change, with no view under the old name.
- **Statuses.** Shared: pending, approved, rejected, expired, cancelled. Schema change only:
  applied, failed, stale. "Decided" means anything but pending for workflow steps, and anything
  but pending or approved for schema changes, as today.
- **One module** owns creating, notifying (phase 2's "approval needed", sent only to those who
  may decide), expiry, cancellation and resuming. The schema change module keeps spec checking,
  compiling and applying, and calls into it.
- **The approval node.** Reaching it renders the prompt, creates a workflow step approval with
  the rendered prompt and input, and records the step as waiting. The run pauses once nothing
  else can run.

  On decision, the node outputs:

  > approved (boolean), note, decided_by (id and name), status (approved, rejected, expired or
  > cancelled)

  Expiry counts as not approved.
- **Deciding** checks phase 1's approval rules. Drops still need the table names typed.
  Decisions are audited. Cancelling a workflow run cancels its pending approvals and clears
  their notifications.
- **Approvals API.**
  - A list, filterable by kind and status.
  - A single approval. It includes the kind, reason, approver role and whether the caller can
    decide, plus either the schema change fields or the step's workflow, run and node.
  - Impact, for schema changes only.
  - Approve and reject, with an optional note and confirmation.
  - Errors: 403 (role), 404, 409 (already decided), 422 (missing drop confirmation, or impact
    for another kind).
  - They replace the old schema change routes, which are removed.
- **Workflow API.**
  - Create and update accept the schemas, templates, approval node fields and conditions,
    answering 422 per invalid item.
  - Starting a run accepts JSON input when the version has a schema.
  - The run page response gains skipped steps, JSON outputs and approval references.
- **The approval expiry setting** (7 days, per organisation) replaces the schema-change-specific
  one.

**start_workflow**
- **Settings:**
  - the allowed workflows;
  - the wait time: default 120 seconds, maximum 600.
- **A call** takes a workflow and an input. It creates an invocation caused by "start
  workflow", chained to the calling turn's invocation.
- **Checks**, before the run starts:
  - input against the schema;
  - invocable by;
  - rate limits;
  - budgets, charged to the chain's root person.

  Refusals are returned as the tool's result.
- **Results.** The tool waits up to the wait time.
  - **If the run ends**, it returns the output or the error.
  - **Otherwise** it returns the run id with a "will be posted" message. The result goes by
    handback to the turn's conversation, else to the trigger's delivery chat, else only to the
    run page.
- **Refused calls:**
  - the target workflow is already in the chain;
  - the chain would exceed the maximum depth (8, per organisation; shared with event triggers);
  - the target is archived or deleted. This refuses only that call.

### 3.5 Budgets

- **Totals.** A store table of running totals, per calendar month in UTC, for the organisation
  and per person. They're updated in the same transaction as each usage ledger entry, and a
  nightly job reconciles them against the ledger and logs any drift.
- **Budget and allowances.** A monthly organisation budget, in phase 1's estimated cost (pounds
  sterling), with none set by default. Default allowances per role, and per-person overrides.
- **Attribution.** Allowances are charged to the chain's root person: whoever mentioned,
  started by hand, or posted the message that fired a chat-message trigger. Schedules and events
  without a person count only against the organisation budget.
- **Checking.** The check slots into phase 1's order (permission, rate limit, budget, then the
  handoff). A refusal is shown on the card, the trigger, or as the tool's result. Runs in flight,
  and resumes of waiting runs, aren't new work and finish.
- **Notifications.** New kinds "budget warning" (once per threshold per month, at 80%) and
  "budget reached", for admins.
- **Budget API.**
  - Get the budget, this month's spend and the role allowances.
  - Set them (admin only).
  - Set a member's allowance override (admin only).

  Changes take effect at once and are audited.
- **Settings:** warning threshold, 80%.

### 3.6 Event triggers

- **Triggers.** The trigger kind gains "event". Triggers gain:
  - an event type and a filter object;
  - a debounce window;
  - a "fire on own events" flag (default off);
  - a webhook token hash and an encrypted webhook secret;
  - a disabled reason.

  A constraint requires an event type for event triggers.
- **Event types.** Each lists its emitter and payload, then its filter fields.

  | Event type | Emitted when, with | Filter fields |
  | --- | --- | --- |
  | Document ingested | A document becomes ready. Document id, name, content type and source (none for uploads until phase 4). | Source, content types |
  | Table written | The write tool commits, and later syncs. Table, operation, row count and writing agent. | Tables, operations |
  | Schema change applied | An approval is applied. Approval id and tables. | Tables |
  | Chat message | Any message is posted in any chat, including by agents and the post-message tool. Conversation, message, author kind and id, mentions. | Conversation (required), author kinds, mentions, keywords (whole words, any case) |
  | Webhook received | The webhook endpoint accepts a call. Delivery id and body. | None |

- **The outbox.** A store events table, written in the same transaction as the change. Each event
  carries:
  - its type and payload;
  - its causing invocation;
  - its chain, root first: the causing invocation's chain plus that invocation, or empty for a
    person's own action;
  - when it was created and dispatched.

  After commit, the writer stamps a "events pending" time on the store's platform registry row
  and publishes a Redis wake-up. A sweep (every 60 seconds) catches stores whose stamp was lost.
  Dispatched events are pruned after 14 days.
- **The dispatcher** runs on the background lane, with a "drain once" entry point for tests. For
  each flagged store it:
  1. claims undispatched events in order, skipping locked ones;
  2. matches them against enabled event triggers, applying filters and then the loop rules;
  3. fires each match, or adds it to a debounce batch;
  4. marks the events dispatched.

  **Firing** creates an invocation (cause "event", with the event and its chain), applies
  phase 1's checks and step 3.5's, and enqueues it on the executor queue.
  - **Agent targets** get a new one-turn run of the current version.
  - **Workflow targets** get a run whose input is the trigger's template rendered with the event,
    or else the event as JSON. It's validated against the input schema.
- **Debounce.** A store batch table records the trigger, the open and close times, the event ids
  and a dropped count. There's one open batch per trigger. When it closes, the trigger fires
  once, with the events (up to 500) and the dropped count.
- **Loop rules**, in order:
  1. **Own events:** skip if the event's immediate cause targets the trigger's own target,
     unless "fire on own events" is set.
  2. **No repeats:** skip if the trigger already appears in the chain.
  3. **Depth:** skip if the chain exceeds the maximum depth.
  4. **Rate:** skip past the per-trigger rate (60 a minute, per organisation), marking the
     trigger "Rate limited".

  Each refusal sets the trigger's last status and error, and is audited. Mentions in automated
  posts still start nothing.
- **Any chat.** A chat-message trigger may name any chat in the organisation, and so may its
  delivery chat. Its creator needn't be in either.
- **Webhooks.**
  - A public endpoint keyed by the trigger's random token (only its hash is stored). It resolves
    the trigger and organisation without the organisation in the path.
  - Requests must carry a timestamp within 5 minutes and an HMAC-SHA256 signature over the
    timestamp and raw body. An optional delivery id is de-duplicated for 24 hours, in a store
    table.
  - Bodies must be JSON of at most 1 MiB, with a per-token rate limit of 120 a minute.
  - Responses:
    - 202 with the delivery id (dispatch is asynchronous);
    - 401 for a bad signature or timestamp;
    - 404 for an unknown or disabled token;
    - 413, 415 and 429 as usual.
  - Webhook triggers return their URL and secret on create. The secret is shown once, and a
    rotate endpoint replaces it.
- **Trigger API.** Create and update accept the event fields. They answer 422 for unknown filter
  keys. Listing shows the disabled
  reason and the last refusal.
- **Settings:**
  - sweep interval: 60 seconds;
  - batch cap: 500 events;
  - per-trigger rate: 60 a minute;
  - event retention: 14 days;
  - webhook tolerance: 300 seconds;
  - webhook body limit: 1 MiB;
  - webhook de-duplication window: 24 hours;
  - webhook rate: 120 a minute.

### 3.7 Lanes and autoscaling

- **Lanes.** The executor queue's lane (added in phase 1) is interactive or background:
  - **Interactive:** mentions and handbacks.
  - **Background:** schedules, events, workflow nodes, start-workflow and the dispatcher.

  Each executor reserves slots (10 by default) for interactive work, and claims interactive
  work first. Background work never takes reserved slots. Fair claiming between organisations
  applies within each lane.
- **Celery** gets three queues, each with its own worker service:
  - **Ingest:** ingest, then syncs in phase 4.
  - **Urgent maintenance:** applying approvals, restores, table access changes and agent role
    creation.
  - **Routine maintenance:** provisioning retries, migrations, measuring, purging, expiry,
    retention, the event sweep and reconciliation.

  The beat runs on one routine worker, and only enqueues. The provisioning queue is retired once
  drained. Only the two maintenance workers hold the provisioner's credentials.
- **Metrics.** Published every 30 seconds by the beat worker:
  - per lane: queue depth, and the age of the oldest ready item;
  - per Celery queue: depth.

  They go to CloudWatch on AWS, and are logged locally.
- **Autoscaling.**
  - Executors scale on the age of the oldest interactive item (target 5 seconds) and on
    background depth.
  - Ingest workers scale on ingest depth.
  - Every service has a configurable minimum and maximum: one to four, and a minimum of two for
    executors.
  - Scale-in waits 10 minutes, and relies on phase 1's graceful shutdown.

### Rollout order

Each numbered item is one or more changes. Each change removes what it replaces, and
development data is rebuilt with `make seed`.

1. **Continuing runs.** The conversation reference and turn records, with mention code switched
   to conversation runs. Compaction ships off, then on.
2. **Memory.** The table and index entries. Ship the tools, then the tab.
3. **Ownership:**
   1. The ownership, shares and credential tables, the group roles, application-level checks and
      the Data page.
   2. Roles and grants created per store by the store migration command, and verified, with
      writes moving to agent roles.
   3. The read-write role loses write on data tables.
4. **Structured workflows and approvals:**
   1. Schema columns, editor and execution changes.
   2. The approvals API and UI, replacing the schema change routes.
   3. The rename to approvals, dropping the old step reference and setting.
5. **Budgets.** The totals table. Checks, a no-op with no budget set. Then the UI.
6. **Event triggers:**
   1. The trigger columns and the events table.
   2. Emit events with no consumer for one release, to watch volume.
   3. The dispatcher.
   4. The UI.
   5. Webhooks.

   **Undo** is disabling event triggers per organisation.
7. **Lanes and autoscaling.** New queues and workers, while the old provisioning queue drains.
   Retire it. Then conservative scaling policies. This can start any time after phase 1's
   executor work, as load needs it.

## Testing Decisions

### What makes a good test here

- **External behaviour only.**
  - What a person sees through the API or screen;
  - what an agent gets back from a tool;
  - what the database allows or refuses;
  - whether a run started, was skipped or was refused.

  Not internal function calls, the shape of intermediate structures, or exactly when summaries
  are written.
- **Real infrastructure.** Tests use real Postgres (including real store databases and real
  roles), real Redis and S3-compatible storage. The scripted model stands in for the provider.
- **Permissions as matrices.** Each actor and place is tested against each
  outcome, so a gap shows up as a missing cell.

### Seams, highest first

1. **The HTTP API as a signed-in person** (the test client, against real services, with the
   scripted model). This is the primary seam for almost everything:
   - mentions creating turns on the current version, and queueing behind approvals;
   - memory listing, editing and deleting by role;
   - table ownership, shares and transfers, and refused writes;
   - workflow save validation, runs with structured output, conditions, skips and approval
     nodes;
   - the approvals list and decisions;
   - start-workflow results and refusals;
   - budget settings and refusals;
   - trigger creation, including chat-message triggers on any chat;
   - the public webhook endpoint (signature, replay, size, rate).

   Prior art: most of the backend suite, including the workflow, pause, schema change, trigger
   and invitation tests.
2. **The full-stack browser test from phase 0**, extended, not duplicated:
   - a second mention continuing the same run after an edit to the agent;
   - a workflow branching on structured output, pausing at an approval node, and resuming on the
     decision in the Approvals list;
   - a table-written trigger firing once, and its echo refused by the loop rules.
3. **Fake-API browser tests for screens.** Prior art: the existing Playwright specs. New specs
   cover:
   - the Memory tab;
   - table ownership on the Data page;
   - the Approvals tab;
   - the condition and approval node editor;
   - event trigger forms;
   - the Budget section.

   The existing schema change, workflow editor, workflow run and schedule specs are updated.
4. **Narrower seams**, only where the API can't reach:
   - **The workflow run executor entry point**, for node-by-node readiness, skip propagation,
     resume after a pause with skips rebuilt, and compaction across long runs. Prior art: the
     workflow run tests calling the executor entry directly.
   - **Tool invocation**, for remember, forget, memory recall, start-workflow and write refusals
     at the database. Prior art: the data tools tests.
   - **The event dispatcher's "drain once" entry point** (new to this phase), for:
     - each event type emitted in the same transaction as its change, with a rolled-back change
       emitting nothing;
     - each filter;
     - debounce and its cap;
     - each loop rule, including a two-trigger cycle;
     - the rate cap;
     - the sweep recovering a lost pending stamp.
   - **The store migration command**, for creating agent roles and grants per store, the
     approvals rename, and the read-write role losing write on data tables.
   - **CI configuration**, for the escape suite as a required check in the backend shards.

### The escape suite (required to merge, step 3.3)

An adversarial test against a real store. Acting as an agent's database role, and as
model-written SQL through the query tool, it asserts that each of these fails or has no effect:

- resetting the role and then writing another agent's table;
- switching role to the store's read-write, owner or group roles, or to another agent's role;
- setting the role through the configuration function;
- setting the session authorisation;
- making the read-only transaction read-write and then modifying data, including through a
  data-modifying common table expression;
- altering one's own role (password or connection limit);
- calling any security-definer function (the suite also asserts none exists in the store);
- writing conversation content (a chat's messages and briefs, run messages and checkpoints, or
  any agent's memory) through the query tool's role or an agent's own role;
- copying to a program, reading server files, or importing large objects;
- injecting SQL through table, column or filter names passed to the write tool;
- writing a table owned by another agent, one whose owner is archived, or one that's dropped,
  with the application's own check bypassed;
- connecting to another store's database, or to the platform database.

### Load

A scripted burst of background work beside interactive mentions. It asserts interactive claim
latency stays within target. It's run on demand, not per pull request.

## Out of Scope

- **Loops and retries in workflow graphs.** Graphs stay acyclic.
- **Per-agent or per-team read restrictions on data tables.** Every agent still reads every
  table, until teams (roadmap).
- **Code or expressions** in conditions or templates beyond the JSONLogic subset and
  placeholders.
- **Billing,** organisation-specific currencies or billing days, and real limit values. Budgets
  are internal guards with generous defaults (roadmap).
- **Events from external systems other than webhooks.** Slack, Drive and GitHub content arrives
  through phase 4's syncs, which then emit the store events defined here.
- **Changing the ranking or cost of search** for memory recall.
- **Value-based pruning** of conversations (roadmap).

## Further Notes

### Dependencies

- **Phase 0.**
  - The full-stack browser test, extended here.
  - Test sharding: the escape suite runs in the backend shards.
  - Archiving: owners archived, the archive dialog.
  - The organisation in the URL.
- **Phase 1.**
  - Everything in the store: runs, workflows, triggers, chats, approvals.
  - The pooler and turn lock.
  - The unified search index and search tool (memory recall).
  - Retention (turn-by-turn pruning).
  - Roles, approval rules and the audit log.
  - Invocations for every cause, with chains, the usage ledger, per-turn caps and invocable by.
  - The executor queue with its lane column and node-by-node workflows.
- **Phase 2.**
  - Open chats, each with its included agents and workflows.
  - Handback and delivery to chats (start-workflow results, trigger delivery).
  - The post-message tool, whose posts emit chat-message events.
  - Notification kinds, including "approval needed".
- **Phase 4.** It consumes this phase's events (document ingested, table written by syncs) and
  the ingest lane. Syncs write as their source, not as an agent, and are unaffected by table
  ownership.

### Risks

- **Grant drift between ownership records and the database.** Every change re-derives grants
  from the records. The store migration command verifies them per store, and the escape suite
  gates merges.
- **Connection growth from per-agent roles.** Mitigated by the pooler, per-role limits, idle
  eviction, and connecting only for writes.
- **Compaction losing meaning.** It ships off first. Full turns stay searchable, and summaries
  are traced for review.
- **Event volume.** Events are emitted with no consumer for one release to measure it. There are
  per-trigger rate caps, debounce, and event retention.

### Decided after review

1. **Unowned tables** stay writable by any agent, so existing agents keep working, until an editor
   assigns an owner.
2. **Deleting memories.** Only editors and admins edit or delete memories.
3. **Currency and period.** Budgets are in estimated pounds sterling (GBP) per calendar month in
   UTC, until billing exists.
4. **Turn records.** Phase 1 introduces the per-turn record, with its usage. This phase adds the
   version each turn ran on.
5. **Open conversations** (settled in review). Every chat is open to the whole organisation.
   Memory isn't tied to a conversation, triggers can watch and deliver to any chat, and agents'
   roles may read conversation tables.

### Where the decision record, the plan and the code differ

- **The read-write role writes data tables today.** The read-write role's default privileges let
  it write every data table. Step 3.3 removes this. The plan doesn't mention it, but
  ownership can't be enforced without it.
- **Data tools and agent roles.** 0001 D5 says an agent's data tools run as its own role. This
  spec runs only the write tool that way, and keeps model-written SQL on the read-only role. That
  narrows the decision for safety rather than contradicting it.
- **Schema change request storage.** Schema change requests live in the platform database today.
  This spec assumes phase 1 has moved them into the store, as 0001 D1 lists.

# Phase 2: Organisation experience

This spec implements [decision record 0001](../decisions/0001-organisation-database-as-system-of-record.md) (D4, D6, D10 and D14) as steps 2.1–2.5 of [the plan](../../plan/platform-cohesion/plan.md), using the words of [the glossary](../../CONTEXT.md).

## Problem Statement

People in an organisation talk to each other and to their agents in two places, and neither works the way the organisation now needs.

**Two conversation models.**
- Group chats are public or private. A private chat has an owner, who alone can rename it, add or remove people, or delete it. If the owner leaves the organisation, the chat is handed to someone else or deleted.
- New chats are private by default, so most conversations are closed to the rest of the organisation without anyone deciding that. Chats are meant to be the team's shared way into its agents.
- Separately, an agent's run page has its own one-to-one chat, which streams. Group chats don't stream.

**Polling instead of streaming.**
- Group chats ask the server for news every second while open, and every 30 seconds while hidden.
- The chat list polls every five seconds.
- The bell polls every ten seconds.

For people, replies arrive in jumps. For the operator, the polling is a constant load on the API and the database.

**Anything typed reaches the agents.** A message posted in a chat goes straight into the model context of every agent it mentions, and stays there for later turns to read. A prompt injection in a message is never checked.

**Results don't come back well.**
- A workflow mentioned in a chat posts its raw output, however long, or as JSON.
- A scheduled run's result stays on its run page. Nobody hears about it unless they go and look.
- Agents can't post anywhere except as a reply to whoever asked.
- The only notification is for a schema change request.

**The built-in assistant only knows the documents.** It can't answer from chats, past runs or data tables, and it's made lazily, the first time someone creates a chat. Admins can't choose which agents join new chats.

**Agents can't move between organisations.** An editor who built a good agent in one organisation must rebuild it by hand in another.

**Maintaining two of everything.** Developers maintain two chat implementations, owner and hand-over logic, and polling loops. Each new feature has to fit both models.

## Solution

**One kind of conversation: the open chat.** There are no private conversations of any kind.
- Anyone in the organisation can find any chat and read it without joining.
- Posting in a chat joins you to it. People join and leave as they like.
- Each chat includes the agents and workflows chosen for it, and only those can be mentioned there.

**Chat members are equal.** People, agents and workflows in a chat are all chat members, and nobody owns a chat.
- Any chat member can rename a chat, change its description, and choose which agents and workflows it includes.
- Nobody removes a person from a chat. Leaving is the only way out.
- Admins only archive and unarchive chats. Nobody deletes one.

**Everything streams.** An open chat streams new messages, run card updates and replies as they're written. The chat list and the bell update live from a per-person stream. Nothing polls every second. The run page becomes a read-only trace, and talking to one agent happens in a new chat made by **Chat with this agent**.

**Messages are checked before they're stored.** Every message a person posts is classified by Jev, the external prompt-injection classifier phase 1 integrates.
- A flagged message is refused, never stored, and the sender sees why.
- If Jev can't answer within a few seconds, the message is refused with "couldn't check this message, try again".
- Every person's message reaching an agent's model context is wrapped as untrusted, whatever Jev said, in case Jev misses something.

**The organisation assistant.** It replaces the documents assistant:
- it can search everything in the store and query data tables;
- it's made when the organisation is made;
- admins choose which agents join every new chat, and the assistant is the default.

**Results come back to people.**
- Agents get a tool to post into chats they're in.
- Schedules can deliver their results to any chat.
- Workflow results, delivered results and long outputs are summarised by a **handback** before they're posted, with the full output one click away.
- People are notified when a run they caused fails, when what they asked for replies, when they're mentioned, and when an approval needs them. The bell covers every organisation they belong to.

**Export and import.** Editors export an agent or workflow version as a file and import it into another organisation as new, unconnected copies. Data, memory, runs and credentials never travel.

## User Stories

### Chats

1. As a member, I want to see every chat in my organisation, whether I've joined it or not, so that I can find conversations relevant to me.
2. As a member, I want to read a chat's history without joining it, so that I can decide whether it's worth joining.
3. As a member, I want to join a chat in one step, so that it's in my chat list and I see its unread counts.
4. As a member, I want posting in a chat I haven't joined to join me to it, so that I don't need a separate step.
5. As a member, I want to leave a chat in one step, so that it stops showing up as unread for me.
6. As a chat member, I want to rename a chat and change its description, so that it stays accurate without waiting for whoever made it.
7. As a chat member, I want to choose which agents and workflows a chat includes, so that the people there can mention the ones that matter to it.
8. As a member, I want a mention of an agent or workflow that the chat doesn't include to be refused, with an **Add it to this chat** prompt, so that I can bring it in deliberately.
9. As a chat member, I want nobody to be able to remove a person from a chat, so that nobody can push others out of an open conversation.
10. As an admin, I want to archive a chat, so that a finished conversation stops collecting posts but stays readable.
11. As an admin, I want to unarchive a chat, so that a conversation can be picked up again.
12. As a member, I want an archived chat to be read-only, with a banner saying so, so that I know why I can't post.
13. As a member, I want an archived chat's messages to still appear in search, so that its knowledge isn't lost.
14. As a member, I want to filter the chat list to archived chats, so that I can find old conversations.
15. As a member, I want chat names to be unique in my organisation, with a clear error on a clash, so that "#general" means one thing.
16. As an admin, I want renames, archiving and unarchiving recorded in the audit log, so that changes to shared conversations can be traced.
17. As a member, I want there to be no delete button on chats, so that a year of history can't vanish in one click.
18. As a member who leaves my organisation, I want to be taken out of all its chats, with nothing to hand over, so that leaving is clean.

### Checking messages

19. As a member, I want a message Jev flags to be refused with the reason, and left in my composer, so that I know it wasn't posted and why.
20. As a member, I want a message refused with "couldn't check this message, try again" when Jev can't be reached in time, so that nothing unchecked reaches the agents.
21. As an admin, I want flagged messages never stored, so that a prompt injection can't sit in a chat for agents to read later.
22. As an operator, I want the check to time out after a few seconds, so that posting never hangs on Jev.
23. As an admin, I want people's messages wrapped as untrusted in every agent's model context, so that an injection Jev misses still can't pass as the agent's own instructions.

### Streaming

24. As a member, I want new messages to appear in an open chat as soon as they're posted, so that the conversation feels live.
25. As a member, I want to watch a run card move from queued to briefing to running to finished as it happens, so that I know what's going on.
26. As a member, I want an agent's reply to appear as it's written, marked as being written, so that I can start reading straight away.
27. As a member, I want the finished reply to replace the draft in place, so that nothing jumps or duplicates.
28. As a member, I want a draft to disappear and the failure to show if the turn fails, so that I'm not left with half an answer.
29. As a member whose connection drops, I want the chat to catch up with every message I missed, exactly once, when it reconnects, so that I never see gaps or duplicates.
30. As a member who opens a chat mid-reply, I want to see the reply so far, so that I don't wait for the end.
31. As a member, I want my chat list's unread counts to update live, so that I notice new activity without refreshing.
32. As a member, I want my bell to update live, so that notifications reach me when they happen.
33. As a member who leaves an organisation, I want my open views of its chats to close with the reason, so that I'm not left looking at a stale page.
34. As a member viewing a chat that gets archived, I want it to turn read-only in front of me, so that I don't try to post.
35. As a member, I want the app to keep working, more slowly, if live updates are unavailable, and to go back to live updates by itself, so that an outage doesn't break the app.
36. As a member with many tabs open, I want a sensible limit on how many live connections I hold, with the oldest closed first, so that one person can't exhaust the server.
37. As an operator, I want nothing in the browser to poll once a second, so that the API and database aren't loaded by idle tabs.
38. As an operator, I want live connections to send regular heartbeats, so that load balancers and proxies don't cut quiet connections.
39. As an operator, I want a per-process cap on live connections, answered with a retry hint when it's reached, so that a process can't be overwhelmed.
40. As an operator, I want live updates to work across many API processes and executors, so that it scales horizontally.
41. As a developer, I want the database to stay the record, with live updates only as a fast path, so that losing a live message never loses data.

### One chat experience

42. As a member, I want a **Chat with this agent** button on an agent's page that makes a new chat with that agent included, named "<Agent> – <person> – <date>", so that talking to one agent works like any other conversation.
43. As a member, I want to rename a chat made that way, so that I can say what it's about.
44. As a participant, I want **Chat with this agent** to respect who may invoke the agent, so that restricted agents stay restricted.
45. As a member, I want a run page to show the run's trace, usage and version per turn, read-only, so that I can understand what happened without a second chat on it.
46. As a member, I want a run page to link to the chat the run came from, so that I can get back to the context.
47. As a member, I want runs made by the old run page chat to stay readable, so that history isn't lost.
48. As a developer, I want the run page's chat endpoint and empty-run creation removed, so that there's one way to talk to an agent.

### Organisation assistant and default agents

49. As a member of a new organisation, I want an organisation assistant there from the start, so that I can ask questions straight away.
50. As a member, I want the organisation assistant to search documents, chats, past runs and synced content, so that it answers from everything the organisation knows.
51. As a member, I want the organisation assistant to query data tables for figures rather than guess, so that its numbers are right.
52. As a member, I want the organisation assistant to cite where an answer came from, so that I can check it.
53. As a member, I want the organisation assistant to say plainly when nothing answers my question, so that I don't get a made-up answer.
54. As an editor, I want to edit the organisation assistant like any other agent, so that the organisation can tailor it.
55. As an editor whose organisation renamed its documents assistant, I want the upgrade to keep that name, so that our choices are respected.
56. As a member of an existing organisation, I want the documents assistant upgraded to the organisation assistant as a new version, so that it gains the new abilities without losing its history.
57. As a member of an organisation that had no documents assistant, I want one made by the upgrade, so that every organisation has an assistant.
58. As an admin, I want to choose an ordered list of agents that join every new chat, so that the organisation's useful agents are always to hand.
59. As an admin, I want archived agents on that list skipped when a chat is made, and shown as archived on the setting, so that the list fails gracefully.
60. As an admin, I want changes to the default agents to affect only new chats, so that existing conversations aren't changed under people.
61. As a participant, I want default agents I'm not allowed to invoke to still be in new chats, with my mentions of them refused on their cards, so that being present and being invoked are separate.

### `post_message`

62. As an editor, I want to give an agent a tool to post into chats it's a chat member of, so that it can report results where people will see them.
63. As an agent, I want to post to a chat by naming it, so that I can report to the right place.
64. As an agent, I want a clear refusal if I try to post into the chat I'm already answering, so that I don't duplicate my reply.
65. As an agent, I want a clear refusal if I post into an archived chat or one I'm not in, so that I can tell the person why.
66. As a member mentioned by an agent's post, I want to be notified, so that the agent can get my attention.
67. As a member, I want agents and workflows mentioned in an agent's post to start nothing, so that agents can't set off loops.
68. As an operator, I want a configurable cap on posts per turn, so that a confused agent can't flood a chat.
69. As an admin, I want agents' posts recorded as tool calls in the usage ledger, so that I can see what they cost.

### Delivering results

70. As an editor, I want a schedule to deliver its result to a chat, so that the team sees a nightly summary without asking.
71. As an editor, I want the schedule's agent or workflow added to the chat when I choose it as the destination, so that the result comes from a chat member.
72. As an editor, I want the schedule's page to say why a delivery didn't happen, with a prompt to choose again, so that I can fix it.
73. As the person who chose the destination, I want to be notified when a delivery can't happen, so that I find out without checking.
74. As a member, I want a delivered result posted once, when the run ends, not as a card when it starts, so that scheduled jobs don't clutter chats.
75. As a member, I want a failed scheduled run to post a short failure message where its result would have gone, so that the team knows it didn't work.
76. As an editor, I want a scheduled run to still run when its delivery can't happen, so that any other work it does carries on.
77. As an editor, I want deliveries to archived chats refused with a clear reason, so that results don't vanish silently.

### Handback

78. As a member who mentioned a workflow, I want its result posted as a short summary answering what I asked, so that I don't have to read raw output.
79. As a member, I want the full output one click away on the run card, so that nothing is hidden from me.
80. As a member, I want JSON output shown pretty-printed behind that disclosure, so that it's readable.
81. As a member, I want an agent's reply to my mention posted as written, unless it's very long or is JSON, so that ordinary answers aren't rewritten.
82. As a member, I want a run card to say "Summarising" while the handback runs, so that I know the result is coming.
83. As a member, I want the raw output posted with a note if summarising fails, so that I always get the result.
84. As a member, I want a run whose summary failed still marked as succeeded, so that a summarising problem doesn't look like the run failed.
85. As an operator, I want handback to run as its own queued step, retried if an executor dies, so that ending a run never waits on a model call.
86. As an operator, I want the handback model configurable, defaulting to the handoff model, so that I can choose cost and quality.
87. As an admin, I want handback calls recorded in the usage ledger and traced, so that their cost and behaviour are visible.
88. As a developer, I want the handback built for workflows and deliveries now, so that phase 3's agent-started workflows can reuse it.

### Notifications

89. As a member, I want to be notified when a run I started, or mentioned, fails, so that I can act on it.
90. As an editor who created a schedule, I want to be notified, along with the admins, when its run fails, so that broken automation is noticed.
91. As a member, I want to be notified when an agent or workflow I mentioned replies, unless I'm watching the chat, so that I can go and do something else meanwhile.
92. As a member, I want to be notified when someone mentions me, so that I see requests for my attention.
93. As a member, I want never to be notified for mentioning myself, so that the bell isn't noisy.
94. As an editor or admin, I want to be notified when an approval needs me, under the approval rules, so that runs aren't left waiting.
95. As a member, I want an approval's notifications marked read for everyone once it's decided, cancelled or expired, so that the bell stays accurate.
96. As a member, I want reading a chat past a message to mark the reply, mention or failure notifications about it as read, so that I don't have to clear them twice.
97. As a member, I want at most one unread notification about the same thing, so that repeats don't pile up.
98. As a member of several organisations, I want one bell showing notifications from all of them, each labelled with its organisation, so that I don't miss anything.
99. As a member, I want opening a notification from another organisation to switch me there and take me to it, so that it's one click.
100. As a member, I want notifications about things I can no longer see to disappear, so that the bell never points at something I can't open.
101. As a member, I want a **Mark all read** action, so that I can clear the bell quickly.
102. As a member who leaves an organisation, I want its notifications removed from my bell, so that it shows only where I belong.
103. As an operator, I want old read notifications pruned after a configurable time, so that the table doesn't grow for ever.
104. As an admin, I want notifications to hold references, never content, so that what's said stays in the organisation's store.

### Export and import

105. As an editor, I want to export an agent version as a file, so that I can take it to another organisation.
106. As an editor, I want to export a workflow version, with the agents, skills and nested workflows it uses, so that it arrives complete.
107. As an editor, I want an agent or skill used several times in a workflow to be exported once, so that the import doesn't duplicate it.
108. As an editor, I want to import a file into my organisation as new copies, so that our version is ours to change.
109. As an editor, I want imported skills always copied, never matched to ours by name, so that the two organisations stay unconnected.
110. As an editor, I want imported items renamed with "(imported)" when a name is already taken, so that the import never fails on a clash.
111. As an editor, I want a dry run that shows what would be created, renamed or left unrunnable, so that I can check before importing.
112. As an editor, I want an imported agent that needs a tool or connector we don't have to say so and refuse to start, rather than block the import, so that I can set the connector up afterwards.
113. As an admin, I want data, memory, runs, triggers, chats and credentials never to be in an export, so that exporting can't leak them.
114. As an admin, I want an exported organisation assistant to import as an ordinary agent, so that an organisation never ends up with two built-in assistants.
115. As an admin, I want exports and imports recorded in the audit log, so that I can see what came and went.
116. As a participant, I want export and import unavailable to me, so that only builders move agents.
117. As an editor, I want a clear error for a file that isn't an export or is too large, with nothing created, so that a bad file is harmless.

### Developer and operator

118. As a developer, I want the owner and hand-over logic removed, so that the conversation code has one set of rules.
119. As a developer, I want one permission check for chat actions, following the chat rules, so that the API and the screens agree.
120. As a developer, I want the Jev check in the one posting path every person's message goes through, so that a new screen can't skip it.
121. As an operator, I want the proxies' settings checked for live updates (buffering off, timeouts above the heartbeat), so that streams work in every deployment.

## Implementation Decisions

**Prerequisites from earlier phases.** This spec assumes:
- conversations, runs, triggers and invocations live in the organisation's store (phase 1);
- routes are scoped to the organisation in the URL (phase 0);
- every run has an invocation recording its cause, causer and destination (phase 1);
- roles, the audit log, the usage ledger and **invocable by** exist (phase 1);
- unified search exists (phase 1);
- the Jev client, its `classify(content, source)` interface, its fake and untrusted wrapping exist (phase 1);
- agents run on the executor service with its queue and lanes (phase 1).

### Conversation model

**Modules modified:** the chats API module, the chat models, the organisations module (leaving), and the invocations module.

**One kind.** Every conversation is an open chat. There is no conversation kind.
- A chat gains archived fields (when, and by whom).
- A chat's name is required and unique among the organisation's chats. Its description stays.

**What goes.** The owner role on chat members and the public/private visibility are dropped in the same change that stops using them. Nothing is migrated: dev data is rebuilt with `make seed`. The organisations module no longer hands chats over when someone leaves.

**Reading and posting.**
- Anyone in the organisation can read any chat (messages, chat members, run cards) and open its stream without joining.
- Joining is one step. Posting joins the poster in the same transaction as the message.

**Included agents and workflows.** The agents and workflows among a chat's chat members are the ones it includes. Only those can be mentioned or triggered there.
- A message mentioning an agent or workflow the chat doesn't include is refused before it's stored, naming what's missing.
- The screen offers **Add it to this chat**, which joins the person if needed, adds it, and sends the message.

**Leaving.** Anyone can leave a chat at any time, including the last person; the chat stays. Leaving the organisation removes the person from every chat in the same transaction, with nothing handed over.

**Permissions.** One check takes the person, their role and the chat, and answers for each action. The API uses it, and the responses carry the allowed actions so screens can show or hide them. The rules:
- Anyone in the organisation: read, join, post (which joins them).
- Any chat member: leave, rename, describe, add or remove agents and workflows.
- Admins: archive and unarchive. That is their only extra power over chats.
- Nobody: remove a person, or delete a chat.

People join themselves; nobody adds another person to a chat. This spec decides that, since anyone can read and join.

**Archived chats.**
- They refuse posts, mentions, joins, edits and adds with a conflict.
- Deliveries to them fail with a reason.
- Search still finds their messages.
- Their agents' runs already under way finish and post.

### Checking messages with Jev

**Where.** In the one posting path for people's messages, after the permission, archived and mention checks and before anything is written, the message body goes through phase 1's `classify(content, source)` with the source "chat message".

**Flagged.** The post is refused and nothing is written: no message, no invocation, no search item, no notification, nothing on the stream. The response says the message was flagged, and the screen keeps the text in the composer.

**Fails closed.** If Jev errors or doesn't answer within the timeout (a few seconds), the post is refused with "couldn't check this message, try again", and nothing is written.

**Passed.** The message is stored with its verdict, and the verdict is copied onto its search item, as phase 1 does for documents.

**Untrusted wrapping.** Wherever people's messages enter model context (the mentioning message, the chat history the handoff reads, the brief built from them, and the asking message given to handback), each is wrapped in an untrusted block, whatever its verdict.

**Not gated.** Posts by agents and workflows (`post_message`, handback and delivery) don't go through the gate: they're not people's input, and the tool results behind them were classified in phase 1.

### Streaming

**New modules:**
- a stream publisher, used by the API and executors after they commit;
- a stream relay in the API, holding connections and relaying events.

Redis pub/sub carries events between processes. There's one channel per chat (keyed by store and chat) and one per person.

**The chat stream** is a server-sent events endpoint per chat. Its events:

| Event | What it carries |
| --- | --- |
| message | the message as the messages read returns it, with the message id as the event id |
| invocation | the invocation's state |
| draft | the full reply so far, with its invocation |
| chat | the chat's header, including whether it's archived |
| members | the chat members list |
| gone | why the stream is closing: left the organisation, or replaced |
| ping | heartbeat |

**The person stream** is a server-sent events endpoint per person, across all their organisations. Its events:

| Event | What it carries |
| --- | --- |
| chat activity | organisation, chat, newest message id, unread count |
| chat list | a chat appeared, was joined, left, renamed, archived or unarchived |
| notification | new or read, with the unread count |
| ping | heartbeat |

**Catching up** on connect or reconnect, with the last event id:
1. Subscribe first, buffering.
2. Read the messages after that id, the unfinished invocations and any current draft.
3. Send those, then the buffer, dropping message ids already sent.

The client also drops ids it already has. The existing "messages after N" and "before N" reads stay.

**Drafts.**
- While a turn runs, the executor keeps the reply so far in Redis, with an expiry, and publishes the whole text at a capped rate. A missed event therefore costs nothing.
- Drafts never reach the database. The final message replaces the draft.
- On failure, the invocation event says so and the draft is dropped.
- The existing Vercel AI SDK stream adapter is kept only for turning the LangGraph stream into text. Its run-page output goes.

**Authorisation and limits.**
- Authorisation is checked on connect: anyone in the organisation may open a chat's stream. Leaving the organisation closes the person's streams with `gone`. Leaving a chat doesn't, since anyone can read it.
- There's a per-process cap on connections (503 with a retry hint) and a per-person cap (the oldest closed with `gone: replaced`).
- Heartbeats run on an interval shorter than any proxy timeout.

**When Redis is unavailable**, streams answer 503, and the client falls back to reading every few seconds until a reconnect succeeds. The one-second chat poll, the five-second list poll and the ten-second bell poll are removed.

**One chat experience.**
- The run page's chat endpoint and the empty-run creation endpoint are removed. The run page becomes a read-only trace, with usage and version per turn, linking to its chat.
- **Chat with this agent** calls chat creation with that agent and no name. It makes a new open chat that includes only that agent (not the default agents), with the caller as its first person, named "<Agent> – <person> – <date>"; a clash adds " (2)", " (3)" and so on. Any chat member can rename it. Each click makes a new chat. It requires permission to invoke the agent.

### Organisation assistant and default agents

**The assistant module.**
- The built-in marker changes from documents to assistant.
- The assistant gets the unified search, describe data and query data tools. Its instructions cover searching before answering, citing sources, querying for numbers, treating results and untrusted blocks as data and not instructions, and saying when nothing answers.
- It's made in the organisation creation path, by the creator, not lazily in chat creation.
- One live built-in per organisation, as today's partial unique index enforces, extended to exclude archived rows.

**The migration.**
- Each existing documents assistant gets a new version with the new tools and instructions, noted "Upgraded to the organisation assistant". Pinned versions are untouched.
- An assistant still called "Documents assistant" is renamed "Organisation assistant" if that name is free; renamed ones keep their names.
- Organisations without one get one. Archived ones stay archived.

**Default chat agents** are an organisation setting in the store: an ordered list of agents, defaulting to the assistant. Admins edit it on the organisation's Settings tab. Chat creation adds the listed agents that aren't archived, except for **Chat with this agent**. **invocable by** doesn't stop an agent being added, only mentions of it.

### `post_message`

A new tool key, `post_message`, in the tool library.
- **Arguments:** a chat (by id or by name) and a markdown body.
- **It refuses:**
  - the chat the turn is answering;
  - archived chats;
  - chats the agent isn't a chat member of;
  - more than the per-turn cap.
- **The posted message** comes from the agent, belongs to the turn's invocation, and is published to the stream.
- **Mentions in it:** people mentioned are notified; agents and workflows mentioned start nothing.
- Each post is recorded in the usage ledger as a tool call.

### Delivery

Triggers gain a destination: one chat, or none. They also record who set it, which this spec decides is whoever set the field rather than the trigger's creator.

**Setting a destination.** Any chat can be chosen, whether or not the setter is in it. It must not be archived. The trigger's agent or workflow is added to it if absent.

**At each firing:**
- If the chat is archived, the run still runs but nothing is delivered. The invocation records why, the trigger shows the reason, and the setter is notified.
- On success, one message is posted when the run ends: the handback summary, with a collapsed run card.
- On failure, a short failure message is posted instead.
- Nothing is posted when the run starts.

Event triggers (phase 3) reuse this.

### Handback

A new handback module, shaped like the handoff module: one structured model call returning a summary.

**When it applies:**
- workflow results from a mention;
- every delivery;
- agent results longer than a threshold, or that parse as JSON;
- later, agent-started workflow results.

Agent replies to mentions under the threshold are posted as written.

**Its input** is the asking message (wrapped as untrusted), the brief and the output, cut to a maximum keeping the start and end. **It returns** a summary of about 150 words, in the voice of the agent or workflow, keeping names, numbers and links and inventing nothing.

**Flow.**
1. Where the run ends (the workflow end-of-run path and the turn finish path), the invocation is marked "handing back" and the full output stored, in the same transaction.
2. A handback item is enqueued on the interactive lane.
3. The executor runs the handback, posts the summary as the message, stores the summary on the invocation, and marks it succeeded.

**On failure** (model error, timeout, or a malformed answer), it posts the raw output cut to a limit, with a note, and records the error. The invocation still succeeds. A lost item is retried by the queue, and posts raw output once its attempts run out.

Each call is traced, tagged handback, and recorded in the usage ledger with cause handback.

### Notifications

**Platform table changes:** it gains the organisation, and a unique index keeping at most one unread notification per person, kind and reference. Today's schema change request kind becomes "approval needed", with references naming the approval.

| Kind | Recipients | Marked read |
| --- | --- | --- |
| run failed | The invocation's causer. For triggers: the trigger's creator and the admins. For delivery problems: the setter. | Opening the run, or reading the chat past the message |
| reply | The person who mentioned, unless they're watching the chat's stream with the tab visible at that moment | Reading past the message |
| mention | People mentioned by people or by agents' posts; never yourself | Reading past the message |
| approval needed | Everyone who can decide, by the phase 1 rules | Decided, cancelled or expired |

**Rendering.** The bell renders text at read time, by resolving references in each organisation's store, batched per store. References the person can no longer see are dropped and marked read.

**Routes.** The person's notifications move under the person (not the organisation) and list every organisation's. A mark-all-read action is added, and the old routes are removed in the same change.

**Cleanup.** Leaving an organisation deletes its notifications for that person. Read notifications older than the retention setting are pruned daily by the maintenance worker.

### Export and import

A new export module.

**Export** is an editor action on an agent or workflow version, defaulting to the current one, and returns a JSON file. The decision-bearing shape of the file:
- a format name and format version;
- the export time and the root's kind and local reference;
- agents, skills and workflows, each once, keyed by local reference;
- for agents: name, description, model, base prompt, tools (key and settings), and the pinned skills' content;
- for workflows: name, description, layout, nodes referring to local references, edges, and (from phase 3) conditions and schemas;
- connector tools by connector kind and name, with no credentials.

Version ids, data, memory, runs, triggers, chats, credentials, provenance and the built-in marker are never included.

**Import** is an editor action taking a file and a dry-run flag.
- The file is validated against the format, and tool settings through the tool library's lenient loader.
- In one transaction, under the saving locks (skills, then agents, then workflows), it creates new rows at version 1, noted with the file name.
- Names that clash get "(imported)", then "(imported 2)", and so on. Skills are always copied.
- Missing tools or connectors leave versions unrunnable, with the reason on the agent's page.
- A dry run returns what would be created, renamed or left unrunnable, without writing.
- Exports and imports are recorded in the audit log.
- There's a maximum file size.

### Schema changes

Each change ships in one release and removes what it replaces in the same change. There's no expand-and-contract and no data migration of chats: dev data is rebuilt with `make seed`.

**Store, 2.1:**
- archived fields on chats, and the unique index on chat names;
- the Jev verdict on messages;
- visibility, the chat member role and their enum types dropped.

**Store, 2.3:**
- the assistant marker rename, new versions, and missing assistants made;
- the default chat agents table.

**Store, 2.4:**
- on invocations: full output, summary, handback error, delivery error, and a "handing back" status;
- on triggers: destination, and who set it.

**Platform, 2.4:**
- the notification organisation, required;
- the kind rename, and the unread unique index;
- the old reference keys dropped.

Redis holds only pub/sub channels and drafts with an expiry, nothing durable.

### API contract summary

All routes are organisation-scoped, except the person stream and the person's notifications. The house style holds: 404 for what the person may not see, 403 for what they see but may not do, 409 for state conflicts, 422 for bad input.

**Added:**

| Route | Contract |
| --- | --- |
| Archive and unarchive a chat | Admins only. |
| Chat stream | Server-sent events, with the last event id. Anyone in the organisation. |
| Person stream | Server-sent events. |
| The person's notifications | List, mark one read, mark all read. |
| Default chat agents | Read, and admin update. |
| Export an agent or workflow version | Editors and admins. Returns the file. |
| Import | Multipart file and a dry-run flag. |

**Changed:**

| Route | Change |
| --- | --- |
| Chat list | Every chat in the organisation, joined or not, with an archived filter. Loses visibility and the owner flag. Gains archived and whether the person has joined. |
| Create a chat | No visibility. Takes the agents and workflows to include, and adds the default agents. With one agent and no name (**Chat with this agent**), names the chat and adds no default agents. |
| Chat reads | Anyone in the organisation, joined or not. |
| Update a chat | Any chat member. |
| Add a chat member | Agents and workflows only. Any chat member. |
| Remove a chat member | Agents and workflows only. Any chat member. People are never removed. |
| Leave | Allowed for the last person; the chat stays. |
| Post a message | Joins the poster. Refused when archived (409), and when it mentions an agent or workflow the chat doesn't include (422, naming it). Checked by Jev before anything is written: flagged is a 422, an unanswered check is a 503 saying to try again. Mentions notify people. |
| Mark read | Also clears the related notifications. |
| Triggers | Gain the destination, and report delivery problems. |

**Removed:**
- deleting a chat;
- the run page chat;
- creating an empty run;
- the old notification routes.

### Settings and defaults

| Setting | Default |
| --- | --- |
| Jev timeout for chat messages | 3 seconds |
| Stream heartbeat | 15 seconds |
| Streams per API process | 2,000 |
| Streams per person | 20 |
| Draft publish interval | 150 ms |
| Draft expiry | 15 minutes |
| Client fallback read interval | 5 seconds |
| `post_message` posts per turn | 20 |
| Handback model | the handoff model, then the deployment's model |
| Handback threshold for agent results | 3,000 characters |
| Handback input limit | 60,000 characters |
| Raw output limit when handback fails | 8,000 characters |
| Handback timeout | 60 seconds |
| Read notification retention | 90 days |
| Largest import file | 5 MiB |

**Per organisation:** default chat agents, defaulting to the organisation assistant.

### Rollout order

1. **Open chats and the Jev gate (2.1).**
   - Visibility, owner roles and chat deletion go in the same change. Dev data is rebuilt with `make seed`.
   - *Undo:* the previous release, with `make seed`.
2. **Organisation assistant and default agents (2.3).** *Undo:* restore the previous assistant version.
3. **Streaming and the read-only run page (2.2).**
   - The proxy configurations are checked for buffering and timeouts.
   - *Undo:* a client build that uses the fallback read only.
4. **Delivery, handback and notifications (2.4)**, with the old notification routes removed. *Undo:* the previous release.
5. **Export and import (2.5).** Additive.

The assistant migration runs in the store migration command, store by store. A failing store keeps its revision and is retried.

## Testing Decisions

**What makes a good test here.** Tests check external behaviour at the highest seam that reaches it:
- what a person in a given role sees and can do through the API;
- what arrives on a stream;
- what is posted in a chat;
- which notifications exist.

They never check how a module does it: not which helper ran, nor the shape of the Redis messages. Permission tests check the few chat rules directly, not every action for every role: anyone in the organisation reads and joins; posting joins; any chat member renames, describes and sets agents and workflows; nobody removes a person; only admins archive and unarchive; nobody deletes. Jev is replaced by its fake everywhere here; the one contract test against real Jev is phase 1's.

**Seams, highest first:**

1. **The HTTP API**, called as a signed-in person through the test client, against real Postgres, Redis and S3-compatible storage, with the scripted model standing in for the provider. This covers most of the phase:
   - the chat rules and lifecycle (read without joining, join, posting joins, leave, agents and workflows, archive, last person leaving);
   - mentions of agents and workflows the chat doesn't include being refused;
   - the Jev gate: a flagged message refused with nothing stored, a slow or failing Jev refusing with "try again", and people's messages wrapped as untrusted in what the scripted model receives;
   - the organisation assistant answering from search and data;
   - default agents and **Chat with this agent**;
   - `post_message` outcomes, seen through the chat's messages;
   - delivery outcomes;
   - handback results, including failure and raw fallback;
   - notifications for each kind, dedupe, read marking, cross-organisation listing and removal on leaving;
   - export and import round trips, renames, dry runs and errors.

   Prior art: the existing chat, invocation, handoff, documents assistant, notification, organisation and trigger tests.
2. **A server-sent events stream reader in tests (new).** A small helper that opens a stream through the test client and collects events until a condition holds. It tests:
   - ordering;
   - catching up from a last event id with no gaps and no duplicates, including a message published between subscribe and read;
   - drafts growing and being replaced;
   - `gone` on leaving the organisation;
   - 404s for people outside the organisation;
   - limits;
   - Redis being unavailable.
3. **Worker entry points, where the API can't reach:** the executor's drain-the-queue-once entry point from phase 1, to run handback items, including retry after an abandoned lease and raw fallback once attempts run out. Prior art: workflow run tests calling the run executor entry directly.
4. **The store migration command**, to check the assistant migration on a store seeded with today's shapes.
5. **Fake-API browser tests for screens.** Prior art: the existing chat, chat joining, chat tabs, chat messages, mentions, invocations, agent invocations, handoff, documents assistant, notifications and run starters specs. These are updated, and new specs are added for:
   - reading a chat without joining, and posting to join;
   - **Add it to this chat** after a refused mention;
   - a flagged or unchecked message refused and kept in the composer;
   - **Chat with this agent**;
   - archived chats;
   - streaming (drafts, reconnecting, fallback);
   - handback disclosure;
   - default agents;
   - export and import;
   - the read-only run page.

   The fake API gains streams, archiving, included agents, message refusals, defaults and export/import.
6. **The full-stack browser test from phase 0**, extended rather than duplicated:
   - a chat made with **Chat with this agent**, whose reply streams into the browser;
   - a mentioned workflow whose result is posted as a handback, with the full output checked;
   - a member reading a chat they haven't joined, then posting and so joining it.

**Tool behaviour.** `post_message` is tested through turns run with the scripted model, observing what's posted and refused. Prior art: the data tools tests.

## Out of Scope

- Private conversations of any kind. Every chat is open to everyone in the organisation.
- Removing a person from a chat, and deleting chats.
- Building a classifier: aios integrates Jev. Classifying documents and tool results, and the admin flagged list, are phase 1; classifying synced source items is phase 4.
- Editing or deleting messages, threads, reactions, typing indicators and presence.
- Off-app notification delivery (email) and notification preferences (roadmap).
- Continuing runs per conversation, agent memory and compaction (phase 3). In phase 2 each mention still starts a new run.
- Agent-started workflows and posting their later results (phase 3). Handback is built here for them.
- Event triggers (phase 3). Delivery is built here for schedules and reused there.
- Connector set-up and connector tools (phase 4). Exports only carry connector names.
- A template gallery for exported agents (roadmap).

## Further Notes

**Dependencies.**
- [Phase 0](phase-0-refactor-and-guard-rails.md): organisation-scoped routes, multiple organisations, archiving, and the full-stack test.
- [Phase 1](phase-1-foundation.md):
  - conversations and runs in the store;
  - unified search;
  - Jev classification: the client, its interface and fake, verdicts, and untrusted wrapping;
  - roles and the audit log;
  - invocations for every cause;
  - the usage ledger;
  - **invocable by**;
  - the executor, its queue and its drain entry point.
- [Phase 3](phase-3-agents-and-automation.md) reuses delivery for event triggers and handback for agent-started workflows.
- [Phase 4](phase-4-inflow-and-insight.md) gives exports their connector tools, and classifies synced source items with Jev.

**Risks.**
- **Prompt injection from untrusted content.** People's messages reach agents' model context. Mitigation: the Jev gate refuses flagged messages before they're stored, and every message is wrapped as untrusted in model context in case Jev misclassifies.
- **Jev being down stops posting.** The gate fails closed. Mitigation: a short timeout and a clear "try again", so a Jev outage is visible and brief rather than silent.
- **Live update load.** Many idle connections per process. Mitigation: caps, heartbeats, and measuring connections per process before raising limits.
- **Handback changes what people read.** A summary can drop detail. Mitigation: the full output is always one click away, and the threshold is configurable.

**Decided after review**
1. **Skipping reply notifications.** Only a stream whose tab is visible counts as watching. The
   client reports visibility on the stream.
2. **Every conversation is open (settled in review).** Chat is the team's shared way into its
   agents, so direct messages were dropped: there are no private conversations, no copying, and
   nothing in phase 2 hides content from members of the organisation.
3. **The Jev gate fails closed (settled in review).** A message that can't be checked isn't
   posted. Flagged messages are never stored, so later turns can't read them.

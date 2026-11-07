# Phase 2: Organisation experience

This spec implements [decision record 0001](../decisions/0001-organisation-database-as-system-of-record.md) (D4, D6, D10 and D14) as steps 2.1–2.5 of [the plan](../../plan/platform-cohesion/plan.md), using the words of [the glossary](../../CONTEXT.md).

## Problem Statement

People in an organisation talk to each other and to their agents in two places, and neither works the way the organisation now needs.

**Two conversation models.**
- Group chats are public or private. A private chat has an owner, who alone can rename it, add or remove people, or delete it. If the owner leaves the organisation, the chat is handed to someone else or deleted.
- New chats are private by default, so most conversations are closed to the rest of the organisation without anyone deciding that.
- Separately, an agent's run page has its own one-to-one chat, which streams. Group chats don't stream.

**Polling instead of streaming.**
- Group chats ask the server for news every second while open, and every 30 seconds while hidden.
- The chat list polls every five seconds.
- The bell polls every ten seconds.

For people, replies arrive in jumps. For the operator, the polling is a constant load on the API and the database.

**Private isn't private.** Any member of the organisation can read any run, including runs started from a private chat. A private conversation's content can therefore be read from its run pages by people who aren't in it.

**Results don't come back well.**
- A workflow mentioned in a chat posts its raw output, however long, or as JSON.
- A scheduled run's result stays on its run page. Nobody hears about it unless they go and look.
- Agents can't post anywhere except as a reply to whoever asked.
- The only notification is for a schema change request.

**The built-in assistant only knows the documents.** It can't answer from chats, past runs or data tables, and it's made lazily, the first time someone creates a chat. Admins can't choose which agents join new chats.

**Agents can't move between organisations.** An editor who built a good agent in one organisation must rebuild it by hand in another.

**Maintaining two of everything.** Developers maintain two chat implementations, owner and hand-over logic, and polling loops. Each new feature has to fit both models.

## Solution

**Two kinds of conversation.**
- A **chat** is open: anyone in the organisation can find it, read it, join it and leave it.
- A **direct message** is closed: it exists only for its chat members. Nobody outside it can read it, admins included, by any route: the conversation itself, its runs, search, streams or notifications.

**Chat members are equal.** People, agents and workflows in a conversation are all chat members, and nobody owns a conversation.
- Anyone in a chat can rename it and add or remove its agents and workflows.
- Only admins remove people from chats and archive them.
- Anyone in a direct message can add people, agents and workflows. Nobody can remove anyone; leaving is the only way out.
- A direct message can be copied into a new chat, but never turns into one.
- Today's private chats become direct messages.

**Everything streams.** An open conversation streams new messages, run card updates and replies as they're written. The chat list and the bell update live from a per-person stream. Nothing polls every second. The run page becomes a read-only trace, and talking to one agent happens in a direct message.

**The organisation assistant.** It replaces the documents assistant:
- it can search everything in the store and query data tables;
- it's made when the organisation is made;
- admins choose which agents join every new chat, and the assistant is the default.

**Results come back to people.**
- Agents get a tool to post into conversations they're in.
- Schedules can deliver their results to a chat, or to a direct message chosen by someone in it.
- Workflow results, delivered results and long outputs are summarised by a **handback** before they're posted, with the full output one click away.
- People are notified when a run they caused fails, when what they asked for replies, when they're mentioned, and when an approval needs them. The bell covers every organisation they belong to.

**Export and import.** Editors export an agent or workflow version as a file and import it into another organisation as new, unconnected copies. Data, memory, runs and credentials never travel.

## User Stories

### Chats

1. As a member, I want to see every chat in my organisation, whether I've joined it or not, so that I can find conversations relevant to me.
2. As a member, I want to read a chat's history without joining it, so that I can decide whether it's worth joining.
3. As a member, I want to join a chat in one step, so that I can post in it.
4. As a member, I want to leave a chat in one step, so that it stops showing up as unread for me.
5. As a member, I want to be refused when I post in a chat I haven't joined, with a prompt to join, so that I'm not posting by surprise.
6. As a chat member, I want to rename a chat and change its description, so that it stays accurate without waiting for whoever made it.
7. As a chat member, I want to add a person to a chat, so that I can bring a colleague in without them having to find it.
8. As a chat member, I want to add an agent or workflow to a chat, so that the people there can mention it.
9. As a chat member, I want to remove an agent or workflow from a chat, so that the chat's mention list stays relevant.
10. As a chat member, I want to be refused when I try to remove another person from a chat, so that nobody can push others out of an open conversation.
11. As an admin, I want to remove a person from a chat, so that I can deal with disruptive behaviour.
12. As an admin, I want a person I removed from a chat to be able to rejoin, because chats are open, so that removal is a nudge, not a ban.
13. As an admin, I want to archive a chat, so that a finished conversation stops collecting posts but stays readable.
14. As an admin, I want to unarchive a chat, so that a conversation can be picked up again.
15. As a member, I want an archived chat to be read-only, with a banner saying so, so that I know why I can't post.
16. As a member, I want an archived chat's messages to still appear in search, so that its knowledge isn't lost.
17. As a member, I want to filter the chat list to archived chats, so that I can find old conversations.
18. As a member, I want chat names to be unique in my organisation, with a clear error on a clash, so that "#general" means one thing.
19. As an admin, I want renames, archiving, unarchiving and removals recorded in the audit log, so that changes to shared conversations can be traced.
20. As a member, I want there to be no delete button on chats, so that a year of history can't vanish in one click.
21. As a member who leaves my organisation, I want to be taken out of all its conversations, with nothing to hand over, so that leaving is clean.

### Direct messages

22. As a member, I want to start a direct message with any people, agents and workflows in my organisation, so that I can have a closed conversation.
23. As a member, I want a direct message to need no name, and to show its chat members' names when it has none, so that starting one is quick.
24. As a member, I want to give a group direct message an optional name, so that I can tell several apart.
25. As a chat member of a direct message, I want to add people, agents and workflows to it, so that the right participants can join the conversation.
26. As a person added to a direct message, I want to read its history from the start, so that I have the context.
27. As a chat member of a direct message, I want to be sure nobody can remove me, so that a closed conversation can't be taken over.
28. As a chat member of a direct message, I want to leave it whenever I like, so that I'm never stuck in one.
29. As the last person to leave a direct message, I want it deleted with its messages, so that content nobody can ever read again isn't kept.
30. As an admin, I want direct messages to be invisible to me, including that they exist, so that people can trust them to be private.
31. As a member, I want any attempt to open a direct message I'm not in, even with its link, to look exactly like it doesn't exist, so that its existence isn't confirmed.
32. As a member of a direct message, I want runs started from it to be visible only to its chat members, so that its content can't be read on run pages.
33. As a member of a direct message, I want its content to be left out of search for everyone outside it, so that searching can't reveal it.
34. As a member of a direct message, I want notifications about it to name its chat members but never show its content, so that a notification can't leak what was said.
35. As a chat member of a direct message, I want to copy it into a new chat, so that a closed conversation can be opened up deliberately.
36. As a chat member copying a direct message, I want the new chat to start with a transcript of its messages, without run cards or briefs, so that the context carries over without internal detail.
37. As a chat member of a direct message, I want the direct message to stay unchanged after it's copied, with no link back from the chat, so that copying doesn't expose the original.
38. As a member, I want to be refused if I try to turn a direct message into a chat, so that nobody's private words become public without a deliberate copy.
39. As a member, I want direct messages listed separately from chats, so that I can tell open and closed conversations apart at a glance.
40. As a member, I want new direct messages never to get the default agents, so that only who I choose is in them.
41. As a member who leaves my organisation while I'm the only person in a direct message, I want it deleted in the same step, so that nothing unreadable is left behind.

### Migrating today's chats

42. As a member, I want today's public chats to become chats unchanged, so that nothing about them moves.
43. As a member, I want today's private chats to become direct messages with the same people and name, so that they stay closed.
44. As a former chat owner, I want to become an ordinary chat member, so that I can now leave a conversation I used to be stuck owning.
45. As a member, I want the release note to tell me that private chats became direct messages, and how to copy one into a chat if it was meant to be open, so that I'm not surprised.
46. As an operator, I want private chats with no people left in them to be deleted by the migration, so that no orphaned content stays.

### Streaming

47. As a member, I want new messages to appear in an open conversation as soon as they're posted, so that the conversation feels live.
48. As a member, I want to watch a run card move from queued to briefing to running to finished as it happens, so that I know what's going on.
49. As a member, I want an agent's reply to appear as it's written, marked as being written, so that I can start reading straight away.
50. As a member, I want the finished reply to replace the draft in place, so that nothing jumps or duplicates.
51. As a member, I want a draft to disappear and the failure to show if the turn fails, so that I'm not left with half an answer.
52. As a member whose connection drops, I want the conversation to catch up with every message I missed, exactly once, when it reconnects, so that I never see gaps or duplicates.
53. As a member who opens a conversation mid-reply, I want to see the reply so far, so that I don't wait for the end.
54. As a member, I want my chat list's unread counts to update live, so that I notice new activity without refreshing.
55. As a member, I want my bell to update live, so that notifications reach me when they happen.
56. As a member removed from a chat or a direct message, I want my open view of it to close with the reason, so that I'm not left looking at a stale page.
57. As a member viewing a chat that gets archived, I want it to turn read-only in front of me, so that I don't try to post.
58. As a member, I want the app to keep working, more slowly, if live updates are unavailable, and to go back to live updates by itself, so that an outage doesn't break the app.
59. As a member with many tabs open, I want a sensible limit on how many live connections I hold, with the oldest closed first, so that one person can't exhaust the server.
60. As an operator, I want nothing in the browser to poll once a second, so that the API and database aren't loaded by idle tabs.
61. As an operator, I want live connections to send regular heartbeats, so that load balancers and proxies don't cut quiet connections.
62. As an operator, I want a per-process cap on live connections, answered with a retry hint when it's reached, so that a process can't be overwhelmed.
63. As an operator, I want live updates to work across many API processes and executors, so that it scales horizontally.
64. As a developer, I want the database to stay the record, with live updates only as a fast path, so that losing a live message never loses data.

### One chat experience

65. As a member, I want a **Chat with this agent** button on an agent's page that opens my direct message with it, or makes one, so that talking to one agent works like any other conversation.
66. As a member, I want that button to reuse my existing direct message with exactly that agent, so that I don't collect duplicates.
67. As a participant, I want **Chat with this agent** to respect who may invoke the agent, so that restricted agents stay restricted.
68. As a member, I want a run page to show the run's trace, usage and version per turn, read-only, so that I can understand what happened without a second chat on it.
69. As a member, I want a run page to link to the conversation the run came from, if I can see it, so that I can get back to the context.
70. As a member, I want runs made by the old run page chat to stay readable, so that history isn't lost.
71. As a developer, I want the run page's chat endpoint and empty-run creation removed, so that there's one way to talk to an agent.

### Organisation assistant and default agents

72. As a member of a new organisation, I want an organisation assistant there from the start, so that I can ask questions straight away.
73. As a member, I want the organisation assistant to search documents, chats, past runs and synced content, so that it answers from everything the organisation knows.
74. As a member, I want the organisation assistant to query data tables for figures rather than guess, so that its numbers are right.
75. As a member, I want the organisation assistant to cite where an answer came from, so that I can check it.
76. As a member, I want the organisation assistant to say plainly when nothing answers my question, so that I don't get a made-up answer.
77. As an editor, I want to edit the organisation assistant like any other agent, so that the organisation can tailor it.
78. As an editor whose organisation renamed its documents assistant, I want the upgrade to keep that name, so that our choices are respected.
79. As a member of an existing organisation, I want the documents assistant upgraded to the organisation assistant as a new version, so that it gains the new abilities without losing its history.
80. As a member of an organisation that had no documents assistant, I want one made by the upgrade, so that every organisation has an assistant.
81. As an admin, I want to choose an ordered list of agents that join every new chat, so that the organisation's useful agents are always to hand.
82. As an admin, I want archived agents on that list skipped when a chat is made, and shown as archived on the setting, so that the list fails gracefully.
83. As an admin, I want changes to the default agents to affect only new chats, so that existing conversations aren't changed under people.
84. As a participant, I want default agents I'm not allowed to invoke to still be in new chats, with my mentions of them refused on their cards, so that being present and being invoked are separate.

### `post_message`

85. As an editor, I want to give an agent a tool to post into conversations it's a chat member of, so that it can report results where people will see them.
86. As an agent, I want to post to a conversation by naming it, so that I can report to the right place.
87. As an agent, I want a clear refusal if I try to post into the conversation I'm already answering, so that I don't duplicate my reply.
88. As a member of a direct message, I want an agent answering there to be unable to post into any other conversation in the same turn, so that nothing said there is copied out.
89. As an agent, I want a clear refusal if I post into an archived conversation or one I'm not in, so that I can tell the person why.
90. As a member mentioned by an agent's post, I want to be notified if I can see the conversation, so that the agent can get my attention.
91. As a member, I want agents and workflows mentioned in an agent's post to start nothing, so that agents can't set off loops.
92. As an operator, I want a configurable cap on posts per turn, so that a confused agent can't flood a chat.
93. As an admin, I want agents' posts recorded as tool calls in the usage ledger, so that I can see what they cost.

### Delivering results

94. As an editor, I want a schedule to deliver its result to a chat, so that the team sees a nightly summary without asking.
95. As an editor, I want the schedule's agent or workflow added to the chat when I choose it as the destination, so that the result comes from a chat member.
96. As a person in a direct message, I want to be the only kind of person who can make a schedule deliver into it, so that strangers can't post into my private conversation.
97. As the person who chose a direct message as a schedule's destination, I want deliveries to stop if I leave it, so that it doesn't keep reaching into a conversation I'm no longer part of.
98. As an editor, I want the schedule's page to say why a delivery didn't happen, with a prompt to choose again, so that I can fix it.
99. As the person who chose the destination, I want to be notified when a delivery can't happen, so that I find out without checking.
100. As a member, I want a delivered result posted once, when the run ends, not as a card when it starts, so that scheduled jobs don't clutter chats.
101. As a member, I want a failed scheduled run to post a short failure message where its result would have gone, so that the team knows it didn't work.
102. As an editor, I want a scheduled run to still run when its delivery can't happen, so that any other work it does carries on.
103. As an editor, I want deliveries to archived chats refused with a clear reason, so that results don't vanish silently.

### Handback

104. As a member who mentioned a workflow, I want its result posted as a short summary answering what I asked, so that I don't have to read raw output.
105. As a member, I want the full output one click away on the run card, so that nothing is hidden from me.
106. As a member, I want JSON output shown pretty-printed behind that disclosure, so that it's readable.
107. As a member, I want an agent's reply to my mention posted as written, unless it's very long or is JSON, so that ordinary answers aren't rewritten.
108. As a member, I want a run card to say "Summarising" while the handback runs, so that I know the result is coming.
109. As a member, I want the raw output posted with a note if summarising fails, so that I always get the result.
110. As a member, I want a run whose summary failed still marked as succeeded, so that a summarising problem doesn't look like the run failed.
111. As an operator, I want handback to run as its own queued step, retried if an executor dies, so that ending a run never waits on a model call.
112. As an operator, I want the handback model configurable, defaulting to the handoff model, so that I can choose cost and quality.
113. As an admin, I want handback calls recorded in the usage ledger and traced, so that their cost and behaviour are visible.
114. As a developer, I want the handback built for workflows and deliveries now, so that phase 3's agent-started workflows can reuse it.

### Notifications

115. As a member, I want to be notified when a run I started, or mentioned, fails, so that I can act on it.
116. As an editor who created a schedule, I want to be notified, along with the admins, when its run fails, so that broken automation is noticed.
117. As a member, I want to be notified when an agent or workflow I mentioned replies, unless I'm watching the conversation, so that I can go and do something else meanwhile.
118. As a member, I want to be notified when someone mentions me, so that I see requests for my attention.
119. As a member, I want never to be notified for mentioning myself, so that the bell isn't noisy.
120. As an editor or admin, I want to be notified when an approval needs me, under the approval rules, so that runs aren't left waiting.
121. As a member, I want an approval's notifications marked read for everyone once it's decided, cancelled or expired, so that the bell stays accurate.
122. As a member, I want reading a conversation past a message to mark the reply, mention or failure notifications about it as read, so that I don't have to clear them twice.
123. As a member, I want at most one unread notification about the same thing, so that repeats don't pile up.
124. As a member of several organisations, I want one bell showing notifications from all of them, each labelled with its organisation, so that I don't miss anything.
125. As a member, I want opening a notification from another organisation to switch me there and take me to it, so that it's one click.
126. As a member, I want notifications about things I can no longer see to disappear, so that the bell never points at something I can't open.
127. As a member, I want a **Mark all read** action, so that I can clear the bell quickly.
128. As a member who leaves an organisation, I want its notifications removed from my bell, so that it shows only where I belong.
129. As an operator, I want old read notifications pruned after a configurable time, so that the table doesn't grow for ever.
130. As an admin, I want notifications to hold references, never content, so that what's said stays in the organisation's store.

### Export and import

131. As an editor, I want to export an agent version as a file, so that I can take it to another organisation.
132. As an editor, I want to export a workflow version, with the agents, skills and nested workflows it uses, so that it arrives complete.
133. As an editor, I want an agent or skill used several times in a workflow to be exported once, so that the import doesn't duplicate it.
134. As an editor, I want to import a file into my organisation as new copies, so that our version is ours to change.
135. As an editor, I want imported skills always copied, never matched to ours by name, so that the two organisations stay unconnected.
136. As an editor, I want imported items renamed with "(imported)" when a name is already taken, so that the import never fails on a clash.
137. As an editor, I want a dry run that shows what would be created, renamed or left unrunnable, so that I can check before importing.
138. As an editor, I want an imported agent that needs a tool or connector we don't have to say so and refuse to start, rather than block the import, so that I can set the connector up afterwards.
139. As an admin, I want data, memory, runs, triggers, chats and credentials never to be in an export, so that exporting can't leak them.
140. As an admin, I want an exported organisation assistant to import as an ordinary agent, so that an organisation never ends up with two built-in assistants.
141. As an admin, I want exports and imports recorded in the audit log, so that I can see what came and went.
142. As a participant, I want export and import unavailable to me, so that only builders move agents.
143. As an editor, I want a clear error for a file that isn't an export or is too large, with nothing created, so that a bad file is harmless.

### Developer and operator

144. As a developer, I want the owner and hand-over logic removed, so that the conversation code has one set of rules.
145. As a developer, I want one permission check for conversation actions, following the permission table, so that the API and the screens agree.
146. As a developer, I want every route that can expose conversation content to go through one visibility check, so that the direct message rule can't be missed by a new route.
147. As an operator, I want each schema change to ship expand-then-contract, so that every release works with the one before it.
148. As an operator, I want the proxies' settings checked for live updates (buffering off, timeouts above the heartbeat), so that streams work in every deployment.

## Implementation Decisions

**Prerequisites from earlier phases.** This spec assumes:
- conversations, runs, triggers and invocations live in the organisation's store (phase 1);
- routes are scoped to the organisation in the URL (phase 0);
- every run has an invocation recording its cause, causer and destination (phase 1);
- roles, the audit log, the usage ledger and **invocable by** exist (phase 1);
- unified search with a visibility field exists (phase 1);
- agents run on the executor service with its queue and lanes (phase 1).

### Conversation model

**Modules modified:** the chats API module, the chat models, the organisations module (leaving), and the invocations module.

**The kind.**
- A conversation gains a kind, `chat` or `direct_message`, and archived fields (when, and by whom).
- A chat's name is required and unique among the organisation's chats. That's a partial unique index on chats only.
- A direct message's name is optional and needn't be unique. A direct message has no description.

**What goes.** The owner role on chat members and the public/private visibility are removed. The organisations module no longer hands conversations over when someone leaves.

**Leaving.** It removes the person from every conversation. If that leaves a direct message with no people in it, the direct message is deleted in the same transaction, with its messages, invocations and search entries.

**Permissions.** One check takes the person, their role and the conversation, and answers for each action. The API uses it, and the responses carry the allowed actions so screens can show or hide them.

| Action | Any chat member (chat) | Not in it (chat) | Admin (chat) | Any chat member (direct message) |
| --- | --- | --- | --- | --- |
| Read, members, run cards | yes | yes | yes | yes |
| Post, mention | yes | join first | join first | yes |
| Join | — | yes | yes | — |
| Leave | yes | — | yes | yes |
| Rename, describe | yes | no | yes | name only |
| Add a person | yes | no | yes | yes |
| Add an agent or workflow | yes | no | yes | yes |
| Remove an agent or workflow | yes | no | yes | no |
| Remove a person | no | no | yes | nobody |
| Archive, unarchive | no | no | yes | not possible |
| Copy into a new chat | — | — | — | yes |

Admins have no powers in direct messages; to them, a direct message doesn't exist.

**Decisions this spec makes where the record left room:**
- Any chat member may add a person to a chat, because chats are open.
- Nobody can remove an agent or workflow from a direct message, consistent with people.
- The last person leaving a direct message deletes it.

**One visibility check for content.** Everything that can expose a conversation's content resolves it through one shared check:
- the conversation's own reads;
- its streams;
- run pages and run lists, through the run's invocation;
- search;
- notification rendering.

A direct message is a 404 to anyone outside it. That includes runs and workflow runs whose invocation was made in it, which closes today's leak through run pages. Insights may count such runs (tokens, cost, success) but never show their content. Approvals raised from a direct message stay visible in full to those who decide them (record D7).

**Archived chats.**
- They refuse posts, mentions, joins, edits and adds with a conflict.
- Deliveries to them fail with a reason.
- Search still finds their messages.
- Their agents' runs already under way finish and post.

**Copying.** Copying a direct message into a chat creates the chat, with the copier as its first chat member and the default agents, plus one system message holding a transcript of the direct message's text messages (author and time). Run cards, invocations and briefs aren't copied, and nothing links back.

**The migration** runs per store. Public chats become chats, and private chats become direct messages, keeping their name and chat members. Private chats with no people in them are deleted. Owner roles are dropped.

### Streaming

**New modules:**
- a stream publisher, used by the API and executors after they commit;
- a stream relay in the API, holding connections and relaying events.

Redis pub/sub carries events between processes. There's one channel per conversation (keyed by store and conversation) and one per person.

**The conversation stream** is a server-sent events endpoint per conversation. Its events:

| Event | What it carries |
| --- | --- |
| message | the message as the messages read returns it, with the message id as the event id |
| invocation | the invocation's state |
| draft | the full reply so far, with its invocation |
| chat | the conversation's header |
| members | the chat members list |
| gone | why the stream is closing: left, removed, archived, deleted or replaced |
| ping | heartbeat |

**The person stream** is a server-sent events endpoint per person, across all their organisations. Its events:

| Event | What it carries |
| --- | --- |
| chat activity | organisation, conversation, kind, newest message id, unread count |
| chat list | a conversation appeared, was left, renamed, archived or deleted |
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
- Authorisation is checked on connect. Leaving, removal, archiving or leaving the organisation closes the stream with `gone`.
- There's a per-process cap on connections (503 with a retry hint) and a per-person cap (the oldest closed with `gone: replaced`).
- Heartbeats run on an interval shorter than any proxy timeout.

**When Redis is unavailable**, streams answer 503, and the client falls back to reading every few seconds until a reconnect succeeds. The one-second chat poll, the five-second list poll and the ten-second bell poll are removed.

**One chat experience.**
- The run page's chat endpoint and the empty-run creation endpoint are removed. The run page becomes a read-only trace, with usage and version per turn, linking to its conversation where visible.
- **Chat with this agent** calls the direct message creation endpoint with "reuse": it finds the most recently active direct message whose chat members are exactly the caller and that agent, or creates one. It requires permission to invoke the agent.

### Organisation assistant and default agents

**The assistant module.**
- The built-in marker changes from documents to assistant.
- The assistant gets the unified search, describe data and query data tools. Its instructions cover searching before answering, citing sources, querying for numbers, treating results as data and not instructions, and saying when nothing answers.
- It's made in the organisation creation path, by the creator, not lazily in chat creation.
- One live built-in per organisation, as today's partial unique index enforces, extended to exclude archived rows.

**The migration.**
- Each existing documents assistant gets a new version with the new tools and instructions, noted "Upgraded to the organisation assistant". Pinned versions are untouched.
- An assistant still called "Documents assistant" is renamed "Organisation assistant" if that name is free; renamed ones keep their names.
- Organisations without one get one. Archived ones stay archived.

**Default chat agents** are an organisation setting in the store: an ordered list of agents, defaulting to the assistant. Admins edit it on the organisation's Settings tab. Chat creation adds the listed agents that aren't archived, and direct messages never get them. **invocable by** doesn't stop an agent being added, only mentions of it.

### `post_message`

A new tool key, `post_message`, in the tool library.
- **Arguments:** a conversation (by id, or by name for chats) and a markdown body.
- **It refuses:**
  - the conversation the turn is answering;
  - archived conversations;
  - conversations the agent isn't a chat member of;
  - from a turn started in a direct message, any other conversation;
  - more than the per-turn cap.
- **The posted message** comes from the agent, belongs to the turn's invocation, and is published to the stream.
- **Mentions in it:** people mentioned are notified if they can see the conversation; agents and workflows mentioned start nothing.
- Each post is recorded in the usage ledger as a tool call.

### Delivery

Triggers gain a destination: one conversation, or none. They also record who set it, which this spec decides is whoever set the field rather than the trigger's creator.

**Setting a destination.**
- Choosing a direct message requires being in it.
- Choosing a chat requires it not to be archived, and adds the trigger's agent or workflow to it if absent.

**At each firing:**
- If the setter has left the direct message, or the chat is archived, the run still runs but nothing is delivered. The invocation records why, the trigger shows the reason, and the setter is notified.
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

**Its input** is the asking message, the brief and the output, cut to a maximum keeping the start and end. **It returns** a summary of about 150 words, in the voice of the agent or workflow, keeping names, numbers and links and inventing nothing.

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
| run failed | The invocation's causer. For triggers: the trigger's creator and the admins. For delivery problems: the setter. | Opening the run, or reading the conversation past the message |
| reply | The person who mentioned, unless they're watching the conversation's stream with the tab visible at that moment | Reading past the message |
| mention | People mentioned by people or by agents' posts, who can see the conversation; never yourself | Reading past the message |
| approval needed | Everyone who can decide, by the phase 1 rules | Decided, cancelled or expired |

**Rendering.** The bell renders text at read time, by resolving references in each organisation's store, batched per store. References the person can no longer see are dropped and marked read. Direct messages show chat members' names, never content.

**Routes.** The person's notifications move under the person (not the organisation) and list every organisation's. A mark-all-read action is added, and the old routes redirect for one release.

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

Version ids, data, memory, runs, triggers, conversations, credentials, provenance and the built-in marker are never included.

**Import** is an editor action taking a file and a dry-run flag.
- The file is validated against the format, and tool settings through the tool library's lenient loader.
- In one transaction, under the saving locks (skills, then agents, then workflows), it creates new rows at version 1, noted with the file name.
- Names that clash get "(imported)", then "(imported 2)", and so on. Skills are always copied.
- Missing tools or connectors leave versions unrunnable, with the reason on the agent's page.
- A dry run returns what would be created, renamed or left unrunnable, without writing.
- Exports and imports are recorded in the audit log.
- There's a maximum file size.

### Schema changes

**Store, 2.1 release (expand):**
- conversation kind, archived fields;
- name nullable for direct messages, and the partial unique index;
- the backfill of kinds, and deleting private chats with no people.

**Store, 2.1 contract, at least one release later:** drop visibility, the chat member role, and their enum types.

**Store, 2.3:**
- the assistant marker rename, new versions, and missing assistants made;
- the default chat agents table.

**Store, 2.4:**
- on invocations: full output, summary, handback error, delivery error, and a "handing back" status;
- on triggers: destination, and who set it.

**Platform, 2.4:**
- the notification organisation, backfilled from each notification's store and then made required;
- the kind rename, and the unread unique index;
- the old reference keys dropped in a later release.

Redis holds only pub/sub channels and drafts with an expiry, nothing durable.

### API contract summary

All routes are organisation-scoped, except the person stream and the person's notifications. The house style holds: 404 for what the person may not see, 403 for what they see but may not do, 409 for state conflicts, 422 for bad input.

**Added:**

| Route | Contract |
| --- | --- |
| Create a direct message | Chat members, an optional name and a reuse flag. The caller is always added. |
| Archive and unarchive a chat | Admins only. A 404 for direct messages. |
| Copy a direct message into a chat | Any chat member. A name and an optional description. |
| Conversation stream | Server-sent events, with the last event id. |
| Person stream | Server-sent events. |
| The person's notifications | List, mark one read, mark all read. |
| Default chat agents | Read, and admin update. |
| Export an agent or workflow version | Editors and admins. Returns the file. |
| Import | Multipart file and a dry-run flag. |

**Changed:**

| Route | Change |
| --- | --- |
| Conversation list | Has kinds and an archived filter. Loses visibility and the owner flag. Gains kind and archived. |
| Create a chat | No visibility. Adds the default agents. |
| Conversation reads | A 404 for direct messages the person isn't in. |
| Update a conversation | Any chat member, per kind. |
| Add a chat member | Any chat member. |
| Remove a chat member | Chats only; people by admins only. Direct messages always refuse. |
| Join | Chats only. |
| Leave | Allowed for the last person; deletes an emptied direct message. |
| Post a message | Refused when archived; mentions notify people. |
| Mark read | Also clears the related notifications. |
| Run reads and lists | Apply the direct message rule. |
| Triggers | Gain the destination, and report delivery problems. |

**Removed:**
- deleting a chat;
- the run page chat;
- creating an empty run;
- the old notification routes, after the redirect release.

### Settings and defaults

| Setting | Default |
| --- | --- |
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

Each release works with the previous one.

1. **Conversation kinds and permissions (2.1 expand).**
   - Code stops reading visibility and roles.
   - The run visibility rule for direct messages ships in the same release, or the promise that admins can't read direct messages would be false.
   - The release note covers private chats becoming direct messages.
   - *Undo:* the previous release, since the old columns still exist.
2. **Organisation assistant and default agents (2.3).** *Undo:* restore the previous assistant version.
3. **Streaming and the read-only run page (2.2).**
   - The proxy configurations are checked for buffering and timeouts.
   - *Undo:* a client build that uses the fallback read only.
4. **Delivery, handback and notifications (2.4)**, with notification routes redirecting. *Undo:* the previous release.
5. **Export and import (2.5).** Additive.
6. **Contracts.** Drop visibility, roles and the old notification reference keys.

Store backfills run in the store migration command, store by store. A failing store keeps its revision and is retried.

## Testing Decisions

**What makes a good test here.** Tests check external behaviour at the highest seam that reaches it:
- what a person in a given role sees and can do through the API;
- what arrives on a stream;
- what is posted in a conversation;
- which notifications exist.

They never check how a module does it: not which helper ran, nor the shape of the Redis messages. Permission tests are table-driven over role, membership and kind, so the permission table above is the test's source of truth. Isolation tests are adversarial: they try every route that could reveal a direct message, and pass only if all of them answer as if it doesn't exist.

**Seams, highest first:**

1. **The HTTP API**, called as a signed-in person through the test client, against real Postgres, Redis and S3-compatible storage, with the scripted model standing in for the provider. This covers most of the phase:
   - the conversation permission table and lifecycle (join, leave, add, remove, archive, copy, last person leaving);
   - the private chat migration's effects;
   - direct message isolation across conversations, runs, search, notifications, triggers and copying;
   - the organisation assistant answering from search and data;
   - default agents;
   - `post_message` outcomes, seen through the conversation's messages;
   - delivery outcomes;
   - handback results, including failure and raw fallback;
   - notifications for each kind, dedupe, read marking, cross-organisation listing and removal on leaving;
   - export and import round trips, renames, dry runs and errors.

   Prior art: the existing chat, invocation, handoff, documents assistant, notification, organisation and trigger tests.
2. **A server-sent events stream reader in tests (new).** A small helper that opens a stream through the test client and collects events until a condition holds. It tests:
   - ordering;
   - catching up from a last event id with no gaps and no duplicates, including a message published between subscribe and read;
   - drafts growing and being replaced;
   - `gone` on removal and archiving;
   - 404s for outsiders;
   - limits;
   - Redis being unavailable.
3. **Worker entry points, where the API can't reach:** the executor's drain-the-queue-once entry point from phase 1, to run handback items, including retry after an abandoned lease and raw fallback once attempts run out. Prior art: workflow run tests calling the run executor entry directly.
4. **The store migration command**, to check the private-chat and assistant migrations on a store seeded with today's shapes.
5. **Fake-API browser tests for screens.** Prior art: the existing chat, chat joining, chat tabs, chat messages, mentions, invocations, agent invocations, handoff, documents assistant, notifications and run starters specs. These are updated, and new specs are added for:
   - direct messages;
   - archived chats;
   - streaming (drafts, reconnecting, fallback);
   - handback disclosure;
   - default agents;
   - export and import;
   - the read-only run page.

   The fake API gains streams, conversation kinds, archiving, defaults and export/import.
6. **The full-stack browser test from phase 0**, extended rather than duplicated:
   - a direct message with an agent whose reply streams into the browser;
   - a mentioned workflow whose result is posted as a handback, with the full output checked;
   - an admin failing to open a direct message by its link.

**Tool behaviour.** `post_message` is tested through turns run with the scripted model, observing what's posted and refused. Prior art: the data tools tests.

## Out of Scope

- Editing or deleting messages, threads, reactions, typing indicators and presence.
- Off-app notification delivery (email) and notification preferences (roadmap).
- Continuing runs per conversation, agent memory and compaction (phase 3). In phase 2 each mention still starts a new run.
- Agent-started workflows and posting their later results (phase 3). Handback is built here for them.
- Event triggers (phase 3). Delivery is built here for schedules and reused there.
- Connector set-up and connector tools (phase 4). Exports only carry connector names.
- A template gallery for exported agents (roadmap).
- Per-direct-message retention (roadmap).

## Further Notes

**Dependencies.**
- [Phase 0](phase-0-refactor-and-guard-rails.md): organisation-scoped routes, multiple organisations, archiving, and the full-stack test.
- [Phase 1](phase-1-foundation.md):
  - conversations and runs in the store;
  - unified search with visibility;
  - roles and the audit log;
  - invocations for every cause;
  - the usage ledger;
  - **invocable by**;
  - the executor, its queue and its drain entry point.
- [Phase 3](phase-3-agents-and-automation.md) reuses delivery for event triggers, handback for agent-started workflows, and the visibility check for memory.
- [Phase 4](phase-4-inflow-and-insight.md) gives exports their connector tools, and gives Insights the counts of direct message runs without content.

**Risks.**
- **Most existing chats become direct messages.** Today new chats are private by default, so most organisations' chats will become closed direct messages. Mitigation: the release note, and copy-to-chat.
- **A new route forgets the direct message rule.** Mitigation: one shared visibility check, plus the adversarial isolation test as a merge gate.
- **Live update load.** Many idle connections per process. Mitigation: caps, heartbeats, and measuring connections per process before raising limits.
- **Handback changes what people read.** A summary can drop detail. Mitigation: the full output is always one click away, and the threshold is configurable.

**Decided after review**
1. **Tracing direct-message turns.** They're traced to Langfuse like any other turn, and the docs
   say that whoever operates the deployment can read those traces. Redaction is a possible
   organisation setting later.
2. **Skipping reply notifications.** Only a stream whose tab is visible counts as watching. The
   client reports visibility on the stream.

**Open question**
1. **Agents posting into direct messages unprompted.** A turn started in a chat could post into a
   direct message the agent is in, because nothing private leaves the direct message. Allow it,
   or limit `post_message` from a chat turn to chats?

# Phase 4: Inflow and insight

This phase follows [decision record 0001](../decisions/0001-organisation-database-as-system-of-record.md)
(D2 and D9), the [platform cohesion plan](../../plan/platform-cohesion/plan.md) and the glossary,
[`CONTEXT.md`](../../CONTEXT.md). It covers plan steps **4.1 Sources and connectors**, **4.2 Slack,
Google Drive and GitHub**, and **4.3 Insights**.

## Problem Statement

An organisation's store only knows what its members upload by hand. Its knowledge lives
elsewhere: decisions in Slack threads, policies and plans in Google Drive, and work in GitHub
issues and pull requests. Members ask the organisation assistant or an agent a question, and the
answer is in a place aios can't see. Keeping the store current means someone downloading and
re-uploading files, which nobody does. Uploaded copies go stale and stay after the original is
deleted.

Agents are limited in the same way on the way out. An agent can write to the store's data tables,
but it can't post to the team's Slack channel or comment on the GitHub issue it has just
investigated. So the work stops at the edge of aios, and a person has to copy the result across.

Admins have no single place to see what is coming into the store, from where, whether it is
current, or what happens to it when a connection breaks. They also have nowhere safe to hand aios
credentials for the organisation's other systems.

Nobody in the organisation can see how its agents and workflows are doing:
- **Admins** can't see what the agents cost, which fail, how long approvals wait, or how close the
  store is to its caps without opening runs one at a time.
- **Editors** can't tell which agents and tools are used, or where workflows are slow.
- **Members** who want a specific view ("tokens per agent per week") have no way to get one
  without writing SQL.

The Insights item in the navigation says "Coming soon".

## Solution

**Sources.** Admins add **sources** to the organisation from the Data page. Each source brings
content into the store and keeps it current:
- **Uploads:** everything uploaded today, now named as a source.
- **A URL**, fetched on a schedule.
- **A webhook**, for systems that push.
- **An email inbox**, for forwarded mail.
- **A connector:** a connection to Slack, Google Drive or GitHub.

**Synced content follows its source.** What a source brings in becomes **synced items**. Search,
the organisation assistant, every agent with the search tool, and event triggers all see them.
- When something changes at the source, the synced item is updated.
- When it's deleted there, it's removed from the store.

Each source shows whether it is healthy, when it last synced and what went wrong.

**Connecting is safe.**
- Admins connect a system once, choose exactly which channels, folders or repositories to bring
  in, and are warned plainly when something private at the source will become searchable by
  everyone in the organisation.
- Credentials stay locked away from the store, from screens and from agents.
- Removing a connection stops it at once, and the admin chooses whether its content is deleted or
  kept as ordinary documents.

**Agents act in other systems.** Editors give agents a connector's tools. An agent can post a
summary to a chosen Slack channel, or comment on and open GitHub issues in chosen repositories.
Every such action is signed with the agent's name and recorded in the audit log.

**Insights.** The Insights page shows every member how the organisation's agents and workflows
are doing, using what the store already records:
- turns and success rates;
- tokens, tool calls and estimated cost;
- the most used agents and tools;
- slow and failing workflows;
- approval waiting times;
- chat activity;
- storage against its caps;
- source health.

Anyone can ask for a chart in words. A built-in insights agent writes the query and the chart,
which can be saved for the whole organisation. Charts can only ever show metrics, never anyone's
messages, and never a direct message to someone outside it.

## User Stories

### Sources in general

1. As an admin, I want a Sources tab on the Data page, so that I can see everything that brings
   content into the store in one place.
2. As a member, I want to see the organisation's sources and their health, so that I know what
   the assistant can and can't know about.
3. As an admin, I want only admins to add, change, pause or remove sources, so that what enters
   the store is a deliberate decision.
4. As an editor or participant, I want source actions hidden or disabled for me, with the reason,
   so that I'm not offered something I can't do.
5. As a member, I want everything I've uploaded before to appear under an "Uploads" source, so
   that nothing about my documents changes.
6. As a member, I want to keep uploading documents exactly as I do today, so that the new
   machinery costs me nothing.
7. As an admin, I want Uploads to be impossible to pause or remove, so that uploading can't be
   switched off by accident.
8. As an admin, I want each source to show healthy, delayed, failing or needs-reconnecting, so
   that I can tell at a glance what needs attention.
9. As an admin, I want each source's recent syncs, with counts added, updated, removed and
   skipped, and their errors, so that I can understand what happened.
10. As an admin, I want to sync a source now, or run a full resync, so that I don't have to wait
    for the schedule after fixing something.
11. As an admin, I want to set how often each source syncs, within a minimum, so that busy sources
    stay current without overloading anything.
12. As an admin, I want to pause and resume a source, so that I can stop it temporarily without
    losing its content.
13. As an admin, I want a source that keeps failing to back off and tell me, so that it doesn't
    retry forever unnoticed.
14. As an admin, I want to be notified when a source is failing or needs reconnecting, so that I
    don't discover it weeks later.
15. As an admin, I want a sync that would exceed the storage cap to skip new items and say so,
    while still applying updates and removals, so that being full never leaves deleted content
    behind.
16. As a member, I want synced files and messages to count towards the store's caps as uploads
    and rows do, so that the caps mean the same thing everywhere.
17. As an admin, I want a sync that fails part-way to resume where it left off, without
    duplicating or re-embedding unchanged items, so that failures cost little.
18. As a member, I want an item that changed at its source to be updated in search at the next
    sync, so that answers reflect the current version.
19. As a member, I want an item deleted at its source to disappear from search, so that the
    assistant never quotes something that no longer exists.
20. As a member, I want removals the source's change feed doesn't report to be caught by a
    regular full listing, so that deletions are never missed for long.
21. As a member, I want what agents produced from synced content (messages, memory, rows) to stay
    when the source removes the original, so that the organisation's own work isn't lost.
22. As a member, I want a file I uploaded to stay when the same file is removed from a synced
    source, so that uploads and synced copies are independent.
23. As a member, I want search results to say which source each result came from, with a link to
    the original, so that I can check it.
24. As a member, I want synced documents listed beside uploads on the Documents tab, filterable
    by source, so that I can find them.
25. As a member, I want deleting a synced document to be refused with an explanation, so that I
    don't delete something that comes back at the next sync.
26. As an editor, I want a synced file's ingest to fire the document-ingested event with its
    source, so that triggers can react to synced documents as they do to uploads.
27. As an agent, I want synced content marked as data, not instructions, so that text planted in
    a Slack message or issue can't redirect me.

### URL, webhook and email sources

28. As an admin, I want to add a web page as a source and have it re-fetched on a schedule, so
    that a public policy or price page stays current.
29. As an admin, I want an unchanged page not to be re-ingested, so that schedules don't waste
    work.
30. As an admin, I want a URL that now returns "not found" or "gone" to have its document removed
    while the source keeps trying, so that it behaves like a deletion.
31. As an operator, I want URL sources unable to reach private, loopback, link-local or cloud
    metadata addresses, even through redirects or DNS tricks, so that a source can't be used to
    probe our network.
32. As an admin, I want a clear refusal when a URL isn't reachable from aios, so that I know why
    it can't be added.
33. As an operator, I want URL fetches limited in size, redirects and time, so that one source
    can't tie up a worker.
34. As an admin, I want a webhook source with its own URL and secret, so that another system can
    push events into the store.
35. As an admin, I want the webhook secret shown once and regenerable, so that it isn't exposed
    by revisiting the page.
36. As an operator, I want deliveries with bad signatures, or replays, rejected and stored
    nowhere, so that nobody can inject content without the secret.
37. As an editor, I want every valid webhook delivery to fire the webhook-received event, so that
    a workflow can react to it.
38. As an admin, I want an email source with its own unguessable inbound address, so that people
    can forward mail into the store.
39. As an admin, I want email accepted only from senders I allow and only when it passes sender
    authentication, so that spoofed mail can't plant content.
40. As a member, I want forwarded emails threaded and their attachments ingested as documents, so
    that a forwarded conversation is searchable whole.
41. As an admin, I want to regenerate an email source's address, so that I can cut off an address
    that leaked.

### Connectors

42. As an admin, I want to connect Slack, Google Drive or GitHub from aios, so that their content
    comes in without exporting anything.
43. As an admin, I want to see which connectors this deployment offers, and why any are
    unavailable, so that I'm not left guessing.
44. As an admin, I want one connector per system in the organisation, so that there's one clear
    connection to manage.
45. As an admin, I want to choose exactly which channels, folders or repositories are synced, so
    that only what's relevant comes in.
46. As an admin, I want items that are restricted at the source flagged when I pick them, and a
    warning I must accept that everyone in the organisation will be able to search them, so that
    I never share something private by accident.
47. As a member, I want everything synced to be searchable by everyone in the organisation, so
    that the rules stay simple and match what the admin agreed to.
48. As an admin, I want a connector whose access has been revoked to show "Reconnect" and stop
    syncing, so that it fails visibly.
49. As an admin, I want reconnecting to carry on from where syncing stopped, so that nothing is
    re-imported from scratch.
50. As an admin, I want to remove a connector and choose between deleting its content and keeping
    it as ordinary documents, so that ending a connection doesn't force losing knowledge.
51. As an admin, I want removing a connector to stop its syncs and tools immediately and revoke
    its access at the provider, so that the connection is fully ended.
52. As an operator, I want connector credentials encrypted in the platform database with their
    own key, never in the store, so that agents querying the store can't reach them.
53. As an operator, I want credentials never returned by the API, written to logs or traces, or
    shown to a model, so that one leak can't expose them.
54. As an operator, I want each provider's app settings to be deployment configuration, and a
    provider without them simply unavailable, so that a deployment offers only what it has set
    up.
55. As an operator, I want to switch a provider off for the whole deployment at once, so that I
    can respond to an incident without touching each organisation.
56. As an operator, I want workers to reach only the hosts each enabled provider needs, so that a
    compromised sync can't call arbitrary hosts.
57. As a developer, I want a test provider with a scriptable change feed and listing, so that the
    framework can be built and tested without third parties.

### Connector tools

58. As an editor, I want each connector to appear in the agent editor's tool picker with its
    tools, so that I can give an agent exactly the actions it needs.
59. As an editor, I want an agent version to pin the connector and the tool names it may use, so
    that its behaviour doesn't change when the provider adds tools.
60. As an editor, I want to limit each tool to chosen channels or repositories, so that an agent
    can't post where it shouldn't.
61. As an editor, I want saving an agent with another organisation's connector, or a tool the
    connector doesn't offer, refused, so that versions are always valid.
62. As a participant, I want agents with connector tools to act when I mention them, so that I
    can use what editors built.
63. As an editor, I want **invocable by** to limit who can start an agent with outward-facing
    tools, so that I can keep sensitive agents to fewer people.
64. As a member, I want every post, comment or issue an agent makes outside aios signed with the
    agent's name, so that people at the other end know where it came from.
65. As an admin, I want every outward action recorded in the audit log, with the agent, the
    target, the turn and who caused it, so that I can trace what happened.
66. As an agent, I want to be told when a connector or one of my tools is unavailable, so that I
    can say so instead of failing silently.
67. As an editor, I want a tool that disappears at the provider to be left out of the turn and
    logged, rather than failing the whole turn, so that the agent keeps working.
68. As a member, I want agent versions that depend on a removed connector to refuse to start,
    with the reason, so that nothing runs half-equipped.
69. As an operator, I want connector tool results capped in size, so that one huge response can't
    overwhelm a turn.

### Slack

70. As an admin, I want to pick Slack channels from those the aios app can see, so that I only
    sync what I intend.
71. As an admin, I want to be told a private channel must invite the aios app first, so that I
    know why it isn't listed.
72. As a member, I want Slack messages and their thread replies synced with their channel, author
    name and a link, so that answers can point back to the conversation.
73. As a member, I want edits and deletions in recent Slack history reflected within a short
    window, and older ones caught by the daily reconciliation, so that the store matches Slack.
74. As an admin, I want a full resync to re-read everything, so that I can correct very old
    edits.
75. As an admin, I want a channel removed from scope, archived, or left by the app to have its
    messages removed, so that scope changes take effect.
76. As an operator, I want Slack's rate limits honoured by slowing down rather than failing, so
    that large workspaces sync reliably.
77. As an editor, I want a post-to-Slack tool limited to chosen channels, so that an agent can
    deliver results where the team works.

### Google Drive

78. As an admin, I want to pick Drive folders, including shared drives, and have everything under
    them synced, so that a whole area of documents comes in.
79. As a member, I want Google Docs, Sheets and Slides exported to formats aios can read, so that
    native Google files are searchable.
80. As an admin, I want unsupported and oversized files listed as skipped, so that I know what
    isn't in the store.
81. As a member, I want files moved into a chosen folder added and files moved out removed, so
    that the scope means what it says.
82. As a member, I want an edited Google Doc re-ingested promptly, so that answers use the latest
    text.
83. As an operator, I want Drive shown as unavailable until Google has verified the app for
    production, so that admins don't hit a consent error.

### GitHub

84. As an admin, I want to install the aios GitHub App on chosen repositories and pick from them
    in aios, so that access is limited by GitHub's own permissions.
85. As a member, I want issues, pull requests and their comments synced, with state, labels,
    author and link, so that agents can answer questions about ongoing work.
86. As an admin, I want to optionally sync README and docs files from the default branch, so that
    a project's documentation is searchable.
87. As a member, I want transferred or deleted issues removed at reconciliation, so that the
    store matches GitHub.
88. As an editor, I want comment-on-issue and open-issue tools limited to chosen repositories, so
    that an agent can report findings where the engineers are.

### Insights

89. As a member, I want an Insights page instead of "Coming soon", so that I can see how the
    organisation's agents are doing.
90. As a member, I want turns and workflow runs over time with their success rate, grouped by
    agent, workflow or cause, so that I can spot trends and failures.
91. As a member, I want tokens, tool calls and estimated cost by agent, workflow and cause, so
    that I can see what is expensive.
92. As an admin, I want the cost shown clearly as an estimate, not a bill, so that nobody mistakes
    it for invoicing.
93. As an editor, I want the most used agents and tools, so that I know what to maintain and what
    to retire.
94. As an editor, I want the slowest workflows and nodes and where runs fail, with the latest
    errors, so that I know where to improve.
95. As an admin, I want approval waiting times and approval and rejection rates by kind, so that
    I can see whether approvals hold work up.
96. As a member, I want chat activity (messages, mentions and their outcomes), with direct
    messages counted only as one aggregate and never named, so that private conversations stay
    private.
97. As an admin, I want storage against the caps over time, so that I can see growth before
    writes are refused.
98. As an admin, I want source health on Insights, so that inflow problems are visible alongside
    everything else.
99. As a member, I want turns from before counts were kept shown as "not tracked" rather than
    zero, so that history isn't misleading.
100. As a member, I want a time range and grouping kept in the URL, so that I can share exactly
     the view I'm looking at.
101. As an admin, I want usage by person shown only to admins unless I choose to show it to
     everyone, so that individual usage isn't exposed by default.
102. As a member, I want to ask for a chart in words and get it drawn, so that I don't need SQL.
103. As a member, I want to see the SQL behind any chart, so that I can trust or check it.
104. As a member, I want to save a chart for the whole organisation, so that a useful view
     becomes part of the page.
105. As a member, I want a saved chart to re-run its query on current data when I open it, so
     that it never shows stale numbers.
106. As a chart's creator or an admin, I want to rename or delete a saved chart, so that the page
     stays tidy.
107. As an admin, I want chart saves and deletions in the audit log, so that changes to a shared
     page are traceable.
108. As a member, I want a chart that no longer works to say so and show its SQL, rather than
     disappearing, so that someone can fix it.
109. As a member outside a direct message, I want no chart, however it's worded, to show me its
     content, so that direct messages stay private as promised.
110. As an admin, I want charts unable to read message bodies, briefs, tool inputs and outputs,
     memory or synced text, enforced by the database, so that privacy doesn't depend on the
     model behaving.
111. As an operator, I want chart queries read-only, time-limited and capped in rows and bytes,
     so that one question can't load the store.
112. As an operator, I want chart specs restricted to safe marks and transforms, with no external
     data, links or expressions, so that a chart can't fetch from or send data anywhere.
113. As a member, I want an invalid chart sent back to the agent once to fix before I see an
     error, so that most questions just work.
114. As an admin, I want chart questions rate-limited per person and charged to whoever asked,
     within their allowance and the organisation's budget, so that Insights can't run away with
     cost.
115. As a member, I want panels cached briefly, so that the page is quick and repeated viewing
     doesn't re-run heavy queries.
116. As a member, I want charts drawn in the app's light and dark themes, so that they're
     readable either way.

### Operating and developing

117. As an operator, I want syncs to run on the ingest-and-sync lane under each organisation's
     fair share, so that one organisation's big sync can't delay others.
118. As an operator, I want only one sync per source running at a time, so that syncs never
     overlap or race.
119. As an operator, I want sync records and storage samples pruned after configurable periods,
     so that bookkeeping doesn't grow forever.
120. As a developer, I want every limit and interval in this phase to be a setting with a generous
     default, so that real values can be set from real usage later.
121. As a developer, I want each provider built against recorded responses, with no live calls in
     CI, so that tests are fast and stable.
122. As a developer, I want a fake MCP connector server, so that the tool kind is tested against
     the real protocol without third parties.
123. As a developer, I want the full-stack test to cover a source syncing, an answer from a synced
     item, its removal, and a connector tool's audited action, so that the whole inflow loop is
     guarded.

## Implementation Decisions

### Modules

**Sources** is a new module owning sources, synced items, syncs and reconciliation.
- Its interface is small: create or update a source, run one sync of a source (incremental, full
  or manual), and remove a source with a keep-or-delete choice.
- Each source kind plugs in through one adapter interface, which can:
  - list changes since a cursor;
  - list everything in scope (for reconciliation);
  - fetch an item's content;
  - describe the scope options.
- The sync loop is shared by every kind:
  - it writes items idempotently by external id;
  - it re-indexes only when an item's content hash changes;
  - it advances the cursor only when the sync commits;
  - it removes what a full listing no longer contains.

**Connectors** is a new module owning connectors, their credentials, the connect and reconnect
flows and the provider adapters. Credentials are reached through a single function, which also
refreshes tokens and marks a connector as needing reconnection on a final authorisation failure.
Nothing else reads the credential table.

**Ingest** is modified. File items reuse the existing pipeline unchanged (conversion, chunking,
embedding), with the document now linked to its source and synced item. Message items bypass
document conversion: they're split by tokens and indexed straight into the unified search index
(from step 1.3) as kind `source_item`.

**The tool registry** is modified in one way: a tool spec may declare an organisation-aware check,
run when a version is saved, restored or run. Builds already receive the run's context through
the tool runtime, which is how connector tools find their connector and credentials at call time.

**The `connector` tool kind** is new and registered under the key `connector`, never renamed.
- **Settings:** a connector id and the list of tool names allowed. Each provider tool adds its
  own target restriction (channels or repositories).
- **At the start of each turn:**
  - it lists the connector's tools (from its MCP server, or its native adapter), cached per
    connector for a few minutes;
  - it keeps only the pinned names;
  - it leaves out names that have vanished, and records that in the run's events;
  - it leaves everything out, telling the model why, when the connector is paused, removing or
    needs reconnecting.
- **Results** are capped and carry the not-instructions note the data tools use.
- **Actions with side effects** write an audit entry: tool, target, run, turn, the person behind
  the invocation, and a summary, without the content.

**The MCP client** is new.
- It uses MCP's streamable HTTP transport, inside executor and sync processes.
- Providers use MCP for their tools where a maintained server exists. Bulk sync is always native.
  Which of Slack's and GitHub's tools go through MCP is settled per provider in the step plan,
  after confirming the official servers' availability and auth.
- The client library is pinned exactly, per the dependency policy.

**Provider adapters** are new: Slack, Google Drive, GitHub and a test provider, each implementing
the source adapter and, where it has tools, the tool listing.

**Insights** is a new module:
- fixed panel queries, written by aios and never by a model, read through the existing capped,
  read-only path;
- the built-in **insights agent** (a builtin like the organisation assistant), which returns a
  title, one SQL statement and a Vega-Lite spec;
- a spec validator: the Vega-Lite schema, then an allowlist;
- saved charts.

**Measurement** is modified: the existing store usage measurement also appends a timestamped
sample, so storage can be drawn over time.

### Architectural decisions

- **One upload source per store.** Every store gets exactly one upload source ("Uploads"),
  created by provisioning and backfilled for existing stores. Every uploaded document belongs to
  it, so "which source" is uniform for search filters, documents and events. It can't be paused
  or removed.
- **Two kinds of item.**
  - **File items** become documents. They're stored in S3 under the store's prefix as uploads
    are, and count towards the files quota.
  - **Message items** keep their text on the synced item, cut at a configurable size and marked
    as cut. They count towards the database quota.
- **Upload versus sync.** An upload is the organisation's own and stays until someone deletes
  it. A synced document follows its source. Deleting a synced document directly is refused with
  an explanation, because it would return at the next sync. The way to remove it is at the source,
  or by narrowing the scope.
- **Syncing and reconciling.**
  - Each source syncs incrementally on its own interval.
  - A full listing (reconciliation) runs daily and on demand, to catch deletions a change feed
    misses.
  - One sync per source runs at a time; a second is skipped.
  - Syncs run on the ingest-and-sync lane (D13) under fair claiming. *This replaces D2's mention of
    a dedicated Celery queue with the D13 lane that the plan uses.*
- **Health and failure.**
  - Health is derived from recent syncs: healthy, delayed, failing or needs reconnecting.
  - A source that fails several times in a row backs off to a slow retry and becomes `failing`.
- **Storage caps.** At the cap, a sync skips new items, applies updates and removals, and records
  "storage full".
- **Visibility.** Everything synced is visible organisation-wide (D2). Choosing something
  restricted at the source needs an explicit acknowledgement.
- **Connectors.**
  - One connector per provider per organisation.
  - A connector's sync is a source of kind `connector` pointing at it; its tools are separate.
  - Removing a connector:
    - stops syncs and tools immediately;
    - revokes access at the provider where possible;
    - deletes its credentials;
    - either deletes its content or converts it into Uploads documents. Message items are written
      as Markdown: one document per Slack channel per month, and one per GitHub issue or pull
      request with its comments.
  - Agent versions using a removed connector become unrunnable with a named reason, as versions
    whose tool left the library are today.
- **Credentials.**
  - They live in the platform database, encrypted with a key derived from a new setting distinct
    from the store-password key, so either can be rotated alone.
  - Slack and Google use OAuth authorisation codes, with a signed `state` bound to the admin's
    session, the organisation, the provider and an expiry.
  - GitHub uses a GitHub App installation: the stored credential is the installation id, and
    short-lived installation tokens are minted from the app's private key when needed.
- **Outbound calls.** Sync and tool calls may reach only each enabled provider's configured hosts,
  enforced by the HTTP client. Network-level egress control for workers is out of scope.
- **URL sources** guard against server-side request forgery:
  - only HTTP or HTTPS on ports 80 and 443;
  - the resolved address must be public, checked after every redirect, and the connection is made
    to the checked address;
  - limited redirects, body size and time;
  - conditional requests, and storage only when the content hash changes;
  - "not found" and "gone" count as removal.
- **Webhook sources** reuse the receiving endpoint and signature checks introduced for webhook
  triggers in phase 3. Each valid delivery is stored as a message item and fires the
  webhook-received event. Webhook items are never reconciled.
- **Email sources** have:
  - an unguessable inbound address on a configured domain;
  - an allowed-senders list that is empty by default (nothing accepted);
  - a requirement to pass sender authentication (SPF or DKIM, as reported by the receiving
    service);
  - threading by reply headers, with attachments becoming file items.

  They're available only where the deployment has an inbound mail service.
- **Insights runs on metrics only.**
  - Prompted charts run as a new dedicated read-only store role. It can read only a new `insights`
    schema of views and the organisation's `data` tables.
  - The views expose the panel sources without content: no message bodies, briefs, tool inputs or
    outputs, memory or synced text. Direct messages appear only as an aggregate, and person ids
    appear only where per-person breakdowns are visible to the person asking.
  - This makes "metrics only" a database guarantee. It builds on phase 1's private schemas and
    reporting views: conversation content is already unreadable to every data tool role, and the
    `insights` views are built from phase 1's reporting views, narrowed further for charts.
- **Chart queries** go through the same one-statement, read-only, rolled-back, timed-out, reset
  path as the data tools, with higher row and byte caps for charts.
- **Chart specs** are validated against Vega-Lite's schema, then an allowlist:
  - **marks:** bar, line, area, point, rect, arc, tick, rule and text;
  - **transforms:** aggregate, bin, time unit, fold, stack, sort and window;
  - **refused:** external data URLs, link and URL encodings, images, signal parameters and
    expression strings.

  aios always supplies the data inline. A failing spec is returned to the agent once to fix.
- **Rendering.** Charts are drawn in the browser by a pinned Vega-Lite runtime using its CSP-safe
  expression interpreter. *To confirm in the step plan: the interpreter's compatibility with the
  pinned version.*
- **Insights permissions.**
  - Every member sees Insights.
  - Per-person breakdowns are admin-only unless an admin turns on the organisation setting to
    show them to everyone.
  - Asking for a chart is an invocation charged to whoever asked: subject to their allowance and
    the organisation's budget (phase 3), and rate-limited per person.
  - Saved charts belong to the organisation. Their creator or an admin renames or deletes them,
    and both actions are audited.
- **Panel caching.** Panels and saved-chart data are cached per organisation, panel and range for
  a few minutes.

### Schema

**Platform database** (both expand):
- **Connector credentials:** organisation, store, the store's connector id, provider,
  credential kind (OAuth or GitHub installation), the encrypted secret, scopes, expiry, the
  non-secret external account (workspace or installation id), who created it, timestamps, and
  when it was revoked. Unique per store and connector. Deleted with the organisation.
- **Store usage samples:** store, time measured, database bytes and file bytes. Pruned after a
  configurable history period.

**Store** (all expand, except the one contract noted):

| Table | What it holds |
| --- | --- |
| Sources | Kind (`upload`, `url`, `webhook`, `email`, `connector`), name, settings (never secrets), the connector (for kind `connector`), status (`active`, `paused`, `failing`, `needs_reauth`, `removing`), interval, cursor, last reconciliation, failure count, creator, timestamps. |
| Connectors | Provider, name, scope (channels, folders or repositories), status, creator, timestamps. Unique per provider. |
| Synced items | Source, external id (unique per source), parent item, kind (`message` or `file`), title, URL, author name as the source gives it, time updated at the source, content hash, text (message items), document (file items), last seen, timestamps. |
| Source syncs | Source, kind (`incremental`, `full`, `manual`), status, start, finish, counts (added, updated, removed, skipped, dropped), error. Pruned after a configurable history period. |
| Documents (changed) | Gains a source and a synced item. Both start nullable, and the source is backfilled to Uploads. A later release contracts the source to required. |
| Insight charts | Title, the question asked, SQL, spec, creator, timestamps. |
| `insights` views | One view per panel source, without content. Any later migration that changes an underlying table updates these views in the same migration, so saved charts keep working. |

Provisioning, and this phase's store migration, create the new insights role for every store,
with the same connection limit and statement timeout as the read-only role, and credentials made
and stored as the other roles' are.

### API contracts

All routes are under the organisation's path (from phase 0). Errors follow existing conventions:
- **403** for a role that may not act;
- **404** for anything outside the organisation;
- **409** for a conflicting state, or a store that isn't ready;
- **422** for invalid input.

**Sources** (every member may read; only admins write):
- **List sources:** each source's kind, name, status, health, last sync and item counts.
- **One source:** the source and its recent syncs.
- **Create a source** (URL, webhook or email):
  - a webhook's secret, or an email source's address, is returned **once**;
  - an unreachable URL is a 422 with "This address isn't reachable from aios".
- **Update a source:** name, settings, interval (422 below the minimum), and pause or resume (409
  for Uploads).
- **Sync now:** incremental or full. Answers 202, or 409 if a sync is already running.
- **Rotate secret:** regenerates a webhook secret or an email address, returned once.
- **Remove a source:** answers 202. 409 for Uploads.

**Connectors:**
- **Providers:** those offered in this deployment, and why any are unavailable.
- **List connectors:** status, scope and health. Never credentials.
- **Start connecting:** returns a redirect URL for OAuth or the App install. 409 if one exists or
  the provider is unavailable.
- **Callback:** a single route, not organisation-scoped, because providers need a fixed redirect
  address. It validates the signed `state` against the session, returning 400 for a bad or expired
  state and 403 for a mismatched session.
- **Scope options:** searchable, with restricted-at-source flags.
- **Set scope:** a 422 if restricted items are picked without acknowledgement.
- **Tools:** names, descriptions and input schemas, for the agent editor. A 409 when the connector
  needs reconnecting.
- **Remove a connector:** with keep-as-documents true or false. Answers 202.

**Library and documents:**
- **The tool library listing** now includes the `connector` kind and its settings schema.
- **The documents list** gains each document's source and, for synced documents, the source URL,
  plus a source filter.
- **Deleting a synced document** answers 409 with the reason.

**Insights:**
- **Panel data:** by range, grouping and filter. Returns the series and "not tracked" counts. 403
  for a person grouping when hidden from the caller.
- **Ask for a chart:** a question in; a title, SQL, spec, data and truncation flag out, or an
  error with the SQL. 429 past the rate limit. A budget or allowance refusal follows phase 3's
  contract.
- **Saved charts:** list; save (the SQL and spec are checked again); fetch data (409 with the
  error if the query no longer runs); rename and delete (403 unless creator or admin).
- **Organisation settings** gain the per-person visibility switch (admins only).

### Settings and defaults

Every setting has a generous default. "Per source" and "per organisation" mean an admin can
override it there.

| Setting | Default |
| --- | --- |
| Connector credentials key | development value; required in production |
| OAuth state lifetime | 10 minutes |
| Connector tool cache | 5 minutes |
| Connector tool result cap | 64 KiB |
| Allowed provider hosts | per provider |
| Enabled providers | Slack, Google Drive, GitHub |
| Provider app settings (client id and secret; GitHub App id, private key and slug) | empty, which makes that provider unavailable |
| Sync interval | 15 minutes, per source |
| Minimum sync interval | 5 minutes |
| Reconciliation | every 24 hours |
| Failures before `failing` | 5 |
| Retry while failing | every 6 hours |
| Sync history kept | 90 days |
| Message item text cap | 256 KiB |
| Items per sync transaction | 200 |
| URL size cap | 50 MiB |
| URL redirects | 5 |
| URL timeout | 30 seconds |
| Webhook item cap | 256 KiB |
| Inbound email domain | empty, which makes email unavailable |
| Email attachment cap | 25 MiB |
| Slack re-read window | 7 days |
| Slack daily reconciliation window | 90 days |
| Drive file cap | 50 MiB |
| Insights panel cache | 5 minutes |
| Chart rows | 5,000 |
| Chart bytes | 1 MiB |
| Chart questions per person per hour | 30, per organisation |
| Storage history kept | 400 days |
| Per-person Insights visible to everyone | off, per organisation |

### Provider specifics

**Slack.** A Slack app with a bot token.
- **Scopes to confirm:** public and private channel read and history, user read (for names), and
  chat write.
- **Scope picking:** from channels the bot can see. A private channel must invite the bot first,
  and the picker says so.
- **What syncs:** each message, with its channel, author name, time and permalink. Thread replies
  are children of the thread's first message. Files shared in channels aren't synced in this
  phase.
- **Incremental sync:** reads each channel's history since its cursor, plus replies in threads
  with new activity.
- **Edits and deletions:** each sync re-reads a recent window (7 days), and the daily
  reconciliation re-reads a longer one (90 days). Older messages are settled unless an admin runs
  a full resync.
- **Removal:** channels removed from scope, archived, or left by the bot have their items
  removed.
- **Rate limits:** honoured by slowing down. *To confirm: the limits for non-Marketplace apps.*
- **`post_to_slack`:** posts only to allowed channels, as the bot, signed "— <agent name> via
  aios", and returns the permalink. Slack users aren't mapped to aios people (that's on the
  roadmap).

**Google Drive.** An OAuth client with read-only Drive access.
- **Verification:** *to confirm: this is a restricted scope needing Google's verification and a
  security assessment.* Apply when 4.1 starts. Until it's granted, Drive is unavailable in
  production, with the reason shown.
- **Scope:** chosen folders, including shared drives, recursively.
- **What syncs:** Docs, Sheets and Slides are exported to Word, Excel and PowerPoint formats for
  ingest. Unsupported and oversized files are recorded as skipped.
- **Incremental sync:** Drive's change feed, from a stored page token, keeping changes for files
  that are, or were, inside a chosen folder (ancestry cached per sync).
- **Changes:**
  - moved out of scope, trashed, deleted or no longer visible: removed;
  - moved into scope: added;
  - changed checksum or modified time: re-exported and re-ingested.
- **Reconciliation:** lists the chosen folders.
- **Tools:** none in this phase.

**GitHub.** A GitHub App.
- **Permissions to confirm:** metadata read, issues read and write, pull requests read, and
  contents read only when docs syncing is on.
- **Scope:** admins install the App on repositories in GitHub, then pick from those in aios.
  Repositories added to the installation later aren't synced until picked.
- **What syncs:**
  - issues and pull requests, as message items, with state, labels, author and URL;
  - their comments, including review comments, as children;
  - optionally, README and Markdown files under docs on the default branch, as file items.
- **Incremental sync:** everything updated since the cursor, per repository. Docs files re-sync
  when the default branch's tree for those paths changes.
- **Reconciliation:** re-lists issue and pull request numbers and the current docs files.
- **Tools:** comment on an issue or pull request, and open an issue. Each is limited to allowed
  repositories, acts as the App, is signed with the agent's name, and is audited.

### Rollout order

Each release's migrations work with the previous release's code.

1. **4.1a:**
   - the platform credential and storage-sample tables;
   - the store's source, connector, synced-item and sync tables, and the document columns;
   - the Uploads backfill, safe to run again, in the store migration command;
   - URL and webhook sources, and the test provider.
2. **4.1b:** the `connector` tool kind and the MCP client. Email sources, where inbound mail can
   be configured.
3. **4.1c (contract):** each document's source becomes required.
4. **4.2a GitHub**, then **4.2b Slack**, then **4.2c Google Drive** (gated on verification).
5. **4.3a Insights panels:** the `insights` views and role. Can ship any time after step 1.6.
6. **4.3b Insights charts:** the insights agent, asking, saved charts, and the chart runtime.

**Undoing.**
- Removing a provider from the enabled list stops its syncs and tools at once and leaves its data.
- Sources can be paused per organisation.
- Insights can go back behind its stub.
- No table is dropped in this phase, and the only contract (a required source) is reversible.

## Testing Decisions

**A good test here exercises external behaviour.**
- What a member sees through the API, what search returns, what the audit log records, what a
  provider received.
- Never the adapter's internal calls or the shape of intermediate rows.
- Sync tests assert outcomes: an item is searchable, updated or gone; counts and health are right.
  They don't assert how many queries ran.

**Primary seam: the HTTP API**, called as a signed-in person through the test client, against
real Postgres, Redis and S3-compatible storage, with the scripted model. Most of the backend suite
is prior art. Through it:
- **Sources:** creation, permissions per role, the Uploads backfill as seen in the documents list,
  pausing, manual sync, removal with keep or delete, the refusal to delete a synced document.
- **Connectors:**
  - the connect flow with a signed state, and bad, expired and mismatched states;
  - scope picking with the acknowledgement rule;
  - the provider list.
- **Agent configuration:** saving agent versions with connector tools, and being refused another
  organisation's connector or an unknown tool name.
- **Insights:** panel data against seeded stores, including "not tracked"; chart asking and saving
  with the scripted model returning SQL and specs; per-person visibility; rate limiting.

**The sync worker entry point.** Syncs aren't reachable over HTTP, so tests call "run one sync of
this source" directly, as workflow run tests call the run executor's entry. Through it:
- cursor resume after a failure part-way;
- hash skipping;
- reconciliation removals;
- the storage cap;
- backoff into `failing`;
- `needs_reauth` on a final 401;
- `document.ingested` with its source (read through phase 3's event dispatcher "drain once" entry
  point);
- each provider's adapter, against recorded responses.

**Tool invocation**, as the data tools' tests do:
- the `connector` tool kind's listing, pinning and vanished tools;
- an unavailable connector;
- the result cap and not-instructions note;
- refusals by target;
- audit entries.

**New seam: a fake MCP connector server with recorded responses.** It serves tool listings and
calls over MCP's streamable HTTP transport inside the test process. It's used by tool invocation
tests and by the test provider, so the protocol path is real while nothing leaves the machine.

**Recorded provider responses.** Slack, Google Drive and GitHub tests replay recorded HTTP
exchanges, with no live calls in CI. The recordings cover:
- change feeds;
- edits and deletions in and out of their windows;
- moves in and out of scope;
- transfers;
- rate limiting with retry-after;
- tool calls allowed and refused.

**Adversarial tests:**
- **Credentials:** after full syncs and tool calls, no token or secret appears in any API
  response, captured log record, trace metadata, any text or JSON column of the store, or any
  tool listing given to a model.
- **URL sources:** private, loopback, link-local, metadata, IPv6 and IPv4-mapped addresses; a
  redirect to a private address; a DNS answer that changes between check and connect; a
  non-standard port.
- **Webhooks and email:** bad signatures, replays and oversized bodies; spoofed senders and
  failing sender authentication.
- **The insights role:** it can't read message bodies, direct messages, memory, synced text, the
  store's base tables or another store. Every refused spec feature is rejected.

**The full-stack browser test** (phase 0) is extended, not duplicated:
1. An admin adds the test connector and a URL source served by the test stack.
2. A sync runs.
3. The organisation assistant answers from a synced item.
4. The upstream item is deleted, and the next sync removes it from search.
5. An agent's connector tool acts through the test provider, and the audit log shows it.
6. Insights shows the turn.

**Fake-API browser tests** cover the screens, as the existing Playwright specs do:
- the Sources tab and a source's page, adding each kind, connect and reconnect, scope picking with
  the warning, and removal with keep or delete;
- the Documents tab's source column, and no delete for synced documents;
- the connector tool group in the agent editor;
- Insights panels with range and grouping, asking for a chart, a validation error, save, rename,
  delete, showing the SQL, and the per-person switch.

## Out of Scope

- Keeping each synced item's permissions from its source. That's permission-aware sync, on the
  roadmap after single sign-on.
- Connectors beyond Slack, Google Drive and GitHub.
- Google Drive tools, and any writing back into Drive.
- Two-way sync: nothing in the store is written back to a source, except through an agent's tool.
- Real-time push from providers (Slack events, Drive push notifications, GitHub webhooks).
  Syncing polls.
- Syncing files shared in Slack channels.
- More than one connector per provider per organisation.
- Billing: Insights shows estimates only.
- Network-level egress control for workers. Allowed hosts are enforced in the HTTP client.

## Further Notes

**Dependencies on other phases.**
- **[Phase 0](phase-0-refactor-and-guard-rails.md):** organisation-scoped routes; the full-stack
  test and its sharding.
- **[Phase 1](phase-1-foundation.md):**
  - the store as system of record;
  - the unified search index and `search` tool;
  - roles and the audit log;
  - invocations for every cause and the usage ledger (Insights' main source);
  - the executor and fair queue.
- **[Phase 2](phase-2-organisation-experience.md):** notifications, and the organisation assistant
  that answers from synced items.
- **[Phase 3](phase-3-agents-and-automation.md):**
  - approvals (the approvals panel);
  - budgets and allowances (charging chart questions);
  - event triggers and the webhook receiving endpoint;
  - the ingest-and-sync lane.

**Risks.**
- Google's verification can take weeks and gates Drive in production. Apply at the start of 4.1.
- Provider rate limits on large workspaces make first syncs slow. The interval and reconciliation
  windows are settings, and health reports "delayed" honestly.
- Any later table change can break the `insights` views, and with them, saved charts. Every
  migration touching those tables must update the views in the same migration.

**Direct-message privacy, across phases.** Once D1 moves chats into the store, a query tool reading
the whole store could show direct-message content to someone outside it (D6). Phase 1
(`phase-1-foundation.md`) closes this generally: conversation content lives in private store
schemas that no data tool role can read or describe, and agents reach it only through `search`.
Phase 3's per-agent roles inherit the rule. Insights' metrics-only role is a further narrowing on
top.

**Decided after review**
1. **Notifications for sources.** One new notification kind tells admins when a source is
   failing or needs reconnecting.
2. **Usage by person on Insights** is shown to admins only by default, with a setting to show it
   to everyone.
3. **One webhook concept.** Webhook triggers (phase 3) and webhook sources are one concept, with
   a "keep deliveries" option. The step plans for 3.6 and 4.1 settle its shape together.
4. **Email sources** go on the roadmap for deployments without an inbound mail service, rather
   than blocking step 4.1.
5. **Deleting synced documents** stays refused. Remove the item at its source instead.
6. **Costs** on Insights are estimates in pounds sterling (GBP).

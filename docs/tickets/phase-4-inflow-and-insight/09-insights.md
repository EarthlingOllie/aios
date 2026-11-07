# 09: Insights page and every panel

**What to build:** The "Coming soon" Insights page becomes real for every member. It has a time range and grouping kept in the URL, short caching, and panels that read fixed SQL written by aios over metrics-only `insights` views built on phase 1's reporting views:

- turns and workflow runs over time with success rate, by agent, workflow or cause;
- tokens, tool calls and estimated cost in GBP, and the most used agents and tools; usage by person is admin-only unless an admin turns on the setting;
- the slowest workflows and nodes, where runs fail, and approval waiting times and rates;
- chat activity, with direct messages only as an unnamed aggregate;
- storage against its caps over time, from samples the existing measurement now records;
- source health, once the organisation has sources beyond Uploads.

[Spec](../../specs/phase-4-inflow-and-insight.md)

**Blocked by:** [02: Sync engine, file items, source health](02-sync-engine.md), [Phase 1 · 11: Usage ledger in GBP, caps, mention guards](../phase-1-foundation/11-usage-caps-and-mention-guards.md), [Phase 3 · 07: Approvals as one concept, and the approval node](../phase-3-agents-and-automation/07-approvals-and-approval-node.md)

**Status:** ready-for-agent

- [ ] Every member sees the page; the range and grouping survive a reload and a shared link.
- [ ] Turns from before counts were kept are counted, with tokens shown as "not tracked" rather than zero.
- [ ] Cost appears in GBP from the price table, labelled "estimate, not a bill".
- [ ] A non-admin asking for a person grouping gets 403 until an admin turns on the setting; switching the setting is admin-only and audited.
- [ ] Workflow durations exclude waiting time; failure counts link to the failing nodes' latest errors; expired approvals are counted separately.
- [ ] Direct messages are never named or broken down by person in any panel.
- [ ] Storage samples are written at each measurement and pruned after the history period, and the storage panel shows usage against the caps over time.
- [ ] Sync health on Insights matches the Sources tab, and the panel is hidden when the organisation has only Uploads.
- [ ] Panels read only through the read-only path and are cached for the configured time.

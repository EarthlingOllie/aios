# 05: Connector tools and the MCP client

**What to build:** Editors can give an agent a connector's actions. The agent editor shows each connector as a tool group; the agent version pins the connector and the tool names allowed. During a turn the agent sees only those tools: a tool that has vanished upstream is left out and logged, and an unavailable connector leaves all its tools out with the model told why. Results are tool results from outside: each is capped, classified by Jev inline and wrapped as untrusted, as [Phase 1 · 15](../phase-1-foundation/15-jev-classification.md) does for every tool result not on its trusted list. Every action with side effects is recorded in the audit log.

Tools can be served over MCP's streamable HTTP transport. A fake MCP connector server with recorded responses is the test seam, and the test provider can use it.

[Spec](../../specs/phase-4-inflow-and-insight.md)

**Blocked by:** [01: Sources foundation: organisation-aware tool checks, Uploads source](01-sources-foundation.md), [04: Connectors: credentials, allowlist, choosing what syncs, removal](04-connectors.md), [Phase 1 · 07: Roles and permissions on every route](../phase-1-foundation/07-roles-and-permissions.md), [Phase 1 · 15: Jev: classify untrusted content](../phase-1-foundation/15-jev-classification.md), [Phase 1 · 11: Usage ledger in GBP, caps, mention guards](../phase-1-foundation/11-usage-caps-and-mention-guards.md)

**Status:** done

- [x] Saving, restoring or running a version with another organisation's connector, or a tool name the connector doesn't offer, is refused.
- [x] A turn lists and uses only the pinned tools; a tool that vanished upstream is omitted, the turn still runs, and the run's events say so.
- [x] A connector that is paused, being removed or needs reconnecting leaves all its tools out, and the model is told it's unavailable.
- [x] A result over the cap is cut and says so. Every result, over MCP or native, is classified by Jev inline and wrapped as untrusted; a flagged result, or one Jev can't check, reaches the agent as an error.
- [x] A test-provider action with side effects writes an audit entry with the agent, target, turn and the person who caused the invocation, without the content.
- [x] Tool listing and calls work end to end over MCP through the fake MCP server, and the listing cache honours its setting.
- [x] Participants can cause these actions through agents; **invocable by** still limits who can start the agent.
- [x] The MCP client library is pinned exactly.
- [x] The credentials adversarial test passes: after syncs and tool listings, no secret appears in any API response, captured log, trace, store column or model-facing listing.
- [x] Agent versions that used a removed connector refuse to start with "the <provider> connector was removed".

**Notes:** Settings are `connectors`, a list of pins (connector id, provider, tool names), because a version holds each tool key once: one `connector` tool covers every connector an agent uses, each a group in the picker. Tool names offered are checked when settings are new and when a version is restored; a run leaves out what vanished instead of refusing (a removed connector, or one not the organisation's, refuses everywhere). "The run's events" are the turn's notices (`turns.notices`, store revision 0035, shown with the run's turns). The MCP client is `mcp==1.30.0`. Per-tool target restrictions (channels, repositories) come with the providers' own tools (Slack, GitHub).

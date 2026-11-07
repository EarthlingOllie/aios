# 05: Connector tools and the MCP client

**What to build:** Editors can give an agent a connector's actions. The agent editor shows each connector as a tool group; the agent version pins the connector and the tool names allowed. During a turn the agent sees only those tools: a tool that has vanished upstream is left out and logged, and an unavailable connector leaves all its tools out with the model told why. Results are capped and marked as data, not instructions, and every action with side effects is recorded in the audit log.

Tools can be served over MCP's streamable HTTP transport. A fake MCP connector server with recorded responses is the test seam, and the test provider can use it.

[Spec](../../specs/phase-4-inflow-and-insight.md)

**Blocked by:** [01: Sources foundation: organisation-aware tool checks, Uploads source](01-sources-foundation.md), [04: Connectors: credentials, allowlist, choosing what syncs, removal](04-connectors.md), [Phase 1 · 07: Roles and permissions on every route](../phase-1-foundation/07-roles-and-permissions.md)

**Status:** ready-for-agent

- [ ] Saving, restoring or running a version with another organisation's connector, or a tool name the connector doesn't offer, is refused.
- [ ] A turn lists and uses only the pinned tools; a tool that vanished upstream is omitted, the turn still runs, and the run's events say so.
- [ ] A connector that is paused, being removed or needs reconnecting leaves all its tools out, and the model is told it's unavailable.
- [ ] A result over the cap is cut and says so, and every result carries the not-instructions note.
- [ ] A test-provider action with side effects writes an audit entry with the agent, target, turn and the person who caused the invocation, without the content.
- [ ] Tool listing and calls work end to end over MCP through the fake MCP server, and the listing cache honours its setting.
- [ ] Participants can cause these actions through agents; **invocable by** still limits who can start the agent.
- [ ] The MCP client library is pinned exactly.

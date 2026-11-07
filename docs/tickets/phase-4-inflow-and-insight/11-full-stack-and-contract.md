# 11: Inflow full-stack test; every document has a source

**What to build:** The phase 0 full-stack test covers the whole inflow loop: an admin adds the test connector and a URL source, a sync runs, the organisation assistant answers from a synced item, the item is deleted upstream and the next sync removes it, an agent's connector tool acts and the audit log shows it, and Insights shows the turn.

With every path that writes documents now setting a source, each document's source becomes required (the contract step).

[Spec](../../specs/phase-4-inflow-and-insight.md)

**Blocked by:** [03: URL, webhook and email sources](03-url-webhook-email-sources.md), [04: Connectors: credentials, allowlist, choosing what syncs, removal](04-connectors.md), [05: Connector tools and the MCP client](05-connector-tools-and-mcp.md), [09: Insights page and every panel](09-insights.md)

**Status:** ready-for-agent

- [ ] The inflow scenario passes in CI under sharding.
- [ ] Breaking any link in the loop (sync, search, removal, tool action, audit entry, Insights panel) fails the test.
- [ ] The contract migration runs cleanly on stores that have been backfilled.
- [ ] Uploading, synced file items and keep-as-documents removal all still succeed after the contract.
- [ ] A document can no longer be written without a source.

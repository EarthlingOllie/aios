# 11: Inflow full-stack test

**What to build:** The phase 0 full-stack test covers the whole inflow loop: an admin adds the test connector and a URL source, a sync runs, the organisation assistant answers from a synced item, the item is deleted upstream and the next sync removes it, an agent's connector tool acts and the audit log shows it, and Insights shows the turn.

[Spec](../../specs/phase-4-inflow-and-insight.md)

**Blocked by:** [03: URL, webhook and email sources](03-url-webhook-email-sources.md), [04: Connectors: credentials, allowlist, choosing what syncs, removal](04-connectors.md), [05: Connector tools and the MCP client](05-connector-tools-and-mcp.md), [09: Insights page and every panel](09-insights.md), [Phase 2 · 06: Organisation assistant and default chat agents](../phase-2-organisation-experience/06-organisation-assistant.md)

**Status:** ready-for-agent

- [ ] The inflow scenario passes in CI under sharding.
- [ ] Breaking any link in the loop (sync, search, removal, tool action, audit entry, Insights panel) fails the test.
- [ ] Uploading, synced file items and keep-as-documents removal each give documents with a source.

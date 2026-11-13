# 11: Inflow full-stack test

**What to build:** The phase 0 full-stack test covers the whole inflow loop: an admin adds the test connector and a URL source, a sync runs, the organisation assistant answers from a synced item, the item is deleted upstream and the next sync removes it, an agent's connector tool acts and the audit log shows it, and Insights shows the turn.

[Spec](../../specs/phase-4-inflow-and-insight.md)

**Blocked by:** [03: URL, webhook and email sources](03-url-webhook-email-sources.md), [04: Connectors: credentials, allowlist, choosing what syncs, removal](04-connectors.md), [05: Connector tools and the MCP client](05-connector-tools-and-mcp.md), [09: Insights page and every panel](09-insights.md), [Phase 2 · 06: Organisation assistant and default chat agents](../phase-2-organisation-experience/06-organisation-assistant.md)

**Status:** done

- [x] The inflow scenario passes in CI under sharding. (`frontend/fullstack/inflow.spec.ts`, one test, so CI's 2 full-stack shards split it like the rest; its upstream is the test provider's, shared through Redis in the stack (`SOURCES_TEST_FEEDS_SHARED`) and changed through its testing routes. Both shards (`--shard=1/2`, `2/2`) passed locally against the stack run by hand; CI itself not run from here.)
- [x] Breaking any link in the loop (sync, search, removal, tool action, audit entry, Insights panel) fails the test. (Each link is a step waiting for its outcome: the sync's item count, the assistant's cited answer, the removing sync and the assistant then finding nothing, the post seen upstream and the agent's "Posted.", the audit entry, the Insights rows. Checked by breaking each locally: a deletion the change feed doesn't report, an audit entry not committed, a usage panel missing a row, a search that finds nothing; each failed at its step.)
- [x] Uploading, synced file items and keep-as-documents removal each give documents with a source. (In the full-stack story: an upload, the URL source's page and the removed connector's kept content, each on the Documents tab with its source; and in `backend/tests/test_inflow.py`.)

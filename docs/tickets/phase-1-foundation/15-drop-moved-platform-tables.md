# 15: Remove old tables: drop the moved platform tables and the run history export

**What to build:** The old copies go once the store holds everything. The run history export is removed: its code, tasks, outbox, settings and panel, with a migration that drops its copy from every store at any history revision. Writes to both databases stop and the platform tables of every moved domain are dropped, one domain per release in reverse dependency order, each a release after its read switch held. The run columns now derived from turns and the old heartbeat columns are dropped too.

[Spec](../../specs/phase-1-foundation.md)

**Blocked by:** [06: Unified search across documents, chats and runs](06-unified-search.md), [14: Everything on the run service: run-page turns, graceful shutdown, rate limiter, embeddings, API turn path removed](14-everything-on-run-service.md)

**Status:** ready-for-agent

- [ ] The run history export panel, settings and endpoints are gone, and the export's tests are removed with the suite passing.
- [ ] Stores at any history revision migrate cleanly to the drop of the export's copy.
- [ ] The platform database holds only identity, organisations, memberships, invitations, the store registry and its credentials, notifications, and the coordination tables (the due index, the work queue, the audit outbox).
- [ ] The full-stack test passes with no organisation content in the platform database.
- [ ] Each domain's platform tables are dropped only after its read switch has held for a full release.
- [ ] The migration check reports no differences after the contracts.

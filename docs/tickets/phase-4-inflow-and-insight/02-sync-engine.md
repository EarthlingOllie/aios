# 02: Sync engine, file items, source health

**What to build:** Admins can now see sources sync. The shared sync loop runs end to end with a development-only test provider: it reads changes since a cursor, writes items by external id, re-indexes only what changed, and reconciles against a full listing so that deleted items disappear. Message items become searchable through `search`; file items become documents through the existing ingest pipeline, linked to their synced item.

Each source has its own page with recent syncs (counts added, updated, removed and skipped, and errors), Sync now, Full resync, pause and resume. Health (healthy, delayed, failing, needs reconnecting) appears on the Sources list, failing sources back off, and admins get one source-attention notification.

[Spec](../../specs/phase-4-inflow-and-insight.md)

**Blocked by:** [01: Sources foundation: organisation-aware tool checks, Uploads source](01-sources-foundation.md), [Phase 1 · 06: Unified search across documents, chats and runs](../phase-1-foundation/06-unified-search.md), [Phase 1 · 12: Run service: queue, mentioned turns, handoff, scheduler](../phase-1-foundation/12-run-service.md), [Phase 2 · 07: Notifications across organisations: mentions, replies, failures](../phase-2-organisation-experience/07-notifications.md)

**Status:** ready-for-agent

- [ ] A changed item at the source is updated in search at the next sync, and an item missing from the full listing is removed, with its document, chunks and search entries.
- [ ] A sync that fails part-way resumes from the old cursor without duplicating items or re-embedding unchanged ones.
- [ ] Only one sync per source runs at a time; a second is skipped, and Sync now answers 409 while one runs.
- [ ] A synced file becomes a searchable document attributed to its source; the same file uploaded and synced stays as two independent documents.
- [ ] Deleting a synced document answers 409 with "remove it at its source", and the Documents tab offers no delete for it.
- [ ] At the storage cap a sync skips new items, records "storage full" with skipped counts, and still applies updates and removals.
- [ ] Health is derived from recent syncs and shown on the Sources list and each source's page; five failures in a row set the source to failing and slow its retries.
- [ ] Admins, and only admins, receive one source-attention notification when a source becomes failing or needs reconnecting.
- [ ] Only admins can create, sync, pause or resume a source; editors and participants see these actions disabled with the reason.
- [ ] Syncs run on the run service under each organisation's fair share.

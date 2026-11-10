# 02: Sync engine, file items, source health

**What to build:** Admins can now see sources sync. The shared sync loop runs end to end with a development-only test provider: it reads changes since a cursor, writes items by external id, re-indexes only what changed, and reconciles against a full listing so that deleted items disappear. Message items become searchable through `search`; file items become documents through the existing ingest pipeline, linked to their synced item.

**Jev classification of synced items** lives here, on top of [Phase 1 · 15](../phase-1-foundation/15-jev-classification.md)'s client, verdict columns and flagged list. The sync loop classifies each item when it writes it, and again when its content changes. The verdict is stored on the synced item and copied onto its search item; file items are classified as documents by phase 1's ingest. Flagged items are stored but kept out of search and agents' context until an admin releases them from the flagged list. Items Jev couldn't check stay awaiting check, aren't searchable, and are retried by the next sync. Synced text always reaches a model wrapped as untrusted, whatever its verdict.

Each source has its own page with recent syncs (counts added, updated, removed and skipped, and errors), Sync now, Full resync, pause and resume. Health (healthy, delayed, failing, needs reconnecting) appears on the Sources list, failing sources back off, and admins get one source-attention notification.

[Spec](../../specs/phase-4-inflow-and-insight.md)

**Blocked by:** [01: Sources foundation: organisation-aware tool checks, Uploads source](01-sources-foundation.md), [Phase 1 · 06: Unified search across documents, chats and runs](../phase-1-foundation/06-unified-search.md), [Phase 1 · 15: Jev: classify untrusted content](../phase-1-foundation/15-jev-classification.md), [Phase 1 · 12: Run service: queue, mentioned turns, handoff, scheduler](../phase-1-foundation/12-run-service.md), [Phase 2 · 07: Notifications across organisations: mentions, replies, failures](../phase-2-organisation-experience/07-notifications.md)

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
- [ ] With phase 1's fake classifier, a flagged message item and a flagged file item are stored with their verdict, but `search` doesn't return them and no agent's context includes them. Their search items carry the same verdict.
- [ ] Flagged synced items appear on the flagged list in Settings. An admin releasing one makes it searchable, and the release is recorded in the audit log. Non-admins can't release.
- [ ] While the fake classifier is down, new and changed items are stored as awaiting check and aren't searchable. The next sync retries them, and they become searchable once trusted. The sync record counts items still awaiting check.
- [ ] Synced text given to a model, released items included, is wrapped as untrusted.

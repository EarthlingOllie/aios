# 02: Sync engine, file items, source health

**What to build:** Admins can now see sources sync. The shared sync loop runs end to end with a development-only test provider: it reads changes since a cursor, writes items by external id, re-indexes only what changed, and reconciles against a full listing so that deleted items disappear. Message items become searchable through `search`; file items become documents through the existing ingest pipeline, linked to their synced item.

**Jev classification of synced items** lives here, on top of [Phase 1 · 15](../phase-1-foundation/15-jev-classification.md)'s client, verdict columns and flagged list. The sync loop classifies each item when it writes it, and again when its content changes. The verdict is stored on the synced item and copied onto its search item; file items are classified as documents by phase 1's ingest. Verdicts use phase 1's set (`awaiting_check`, `clean`, `flagged`, `released`). Flagged items are stored but kept out of search and agents' context until an admin releases them from the flagged list. Items Jev couldn't check stay `awaiting_check`, aren't searchable, and are retried by the next sync. A released item that changes goes back to `awaiting_check`, and admins are notified. `document.ingested` fires only for clean items. Synced text always reaches a model wrapped as untrusted, whatever its verdict.

Each source has its own page with recent syncs (counts added, updated, removed and skipped, and errors), Sync now, Full resync, pause and resume. Health (healthy, delayed, failing, needs reconnecting) appears on the Sources list, failing sources back off, and admins get one source-attention notification.

[Spec](../../specs/phase-4-inflow-and-insight.md)

**Blocked by:** [01: Sources foundation: organisation-aware tool checks, Uploads source](01-sources-foundation.md), [Phase 1 · 15: Jev: classify untrusted content](../phase-1-foundation/15-jev-classification.md), [Phase 1 · 12: Run service: queue, mentioned turns, handoff, scheduler](../phase-1-foundation/12-run-service.md), [Phase 2 · 07: Notifications across organisations: mentions, replies, failures](../phase-2-organisation-experience/07-notifications.md), [Phase 3 · 13: Lanes and autoscaling](../phase-3-agents-and-automation/13-lanes-and-autoscaling.md)

**Status:** done

- [x] A changed item at the source is updated in search at the next sync, and an item missing from the full listing is removed, with its document, chunks and search entries.
- [x] A sync that fails part-way resumes from the old cursor without duplicating items or re-embedding unchanged ones.
- [x] Only one sync per source runs at a time; a second is skipped, and Sync now answers 409 while one runs.
- [x] A synced file becomes a searchable document attributed to its source; the same file uploaded and synced stays as two independent documents.
- [x] Deleting a synced document answers 409 with "remove it at its source", and the Documents tab offers no delete for it.
- [x] At the storage cap a sync skips new items, records "storage full" with skipped counts, and still applies updates and removals.
- [x] Health is derived from recent syncs and shown on the Sources list and each source's page; five failures in a row set the source to failing and slow its retries.
- [x] Admins, and only admins, receive one source-attention notification when a source becomes failing or needs reconnecting.
- [x] Only admins can create, sync, pause or resume a source; editors and participants see these actions disabled with the reason.
- [x] Syncs run on the run service's `sources` lane under each organisation's fair share.
- [x] An interval below the minimum sync interval is refused (422).
- [x] Sync records are pruned after the sync history period.
- [x] With phase 1's fake classifier, a flagged message item and a flagged file item are stored with their verdict, but `search` doesn't return them and no agent's context includes them. Their search items carry the same verdict.
- [x] Flagged synced items appear on the flagged list in Settings. An admin releasing one makes it searchable, and the release is recorded in the audit log. Non-admins can't release.
- [x] While the fake classifier is down, new and changed items are stored as awaiting check and aren't searchable. The next sync retries them, and they become searchable once `clean`. The sync record counts items still awaiting check.
- [x] A released item that changes at its source goes back to `awaiting_check`, leaves search until checked, and admins are notified.
- [x] A synced file fires `document.ingested` only once it's `clean`; releasing a flagged one fires nothing.
- [x] Synced text given to a model, released items included, is wrapped as untrusted.

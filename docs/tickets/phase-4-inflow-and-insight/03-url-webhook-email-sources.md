# 03: URL, webhook and email sources

**What to build:** Admins can bring in content from the open web, from systems that push, and from email, as well as uploads.

- **URL sources** fetch a page on their interval with conditional requests and ingest it only when it changes; "not found" or "gone" removes the document. Private, metadata and rebinding targets are refused.
- **Webhook sources** are the single webhook concept with "keep deliveries" on: each valid delivery is stored as a searchable message item and fires `webhook.received`.
- **Email sources** give an unguessable inbound address; mail from allowed senders that passes sender authentication becomes threaded message items, with attachments as documents. Deployments without an inbound mail domain list email as unavailable (it's on the roadmap for them).
- Synced files' ingest fires `document.ingested` carrying their source, so event triggers can filter by source.
- **Jev.** Every item from these sources is classified before it's searchable or fires anything. A webhook delivery uses phase 3's endpoint check: flagged is stored as flagged, fires nothing and answers 422; unchecked answers 503 and isn't stored. URL and email items Jev can't check stay `awaiting_check`, retried by the next fetch or phase 1's awaiting-check sweep.

[Spec](../../specs/phase-4-inflow-and-insight.md)

**Blocked by:** [02: Sync engine, file items, source health](02-sync-engine.md), [Phase 3 · 12: More events: table written, schema applied, chat message, debounce, webhooks](../phase-3-agents-and-automation/12-more-events.md)

**Status:** done

- [x] An unchanged page isn't re-ingested; a changed page replaces its document's content; a 404 or 410 removes the document while the source stays active.
- [x] Every case in the request-forgery suite is refused: private, loopback, link-local and metadata addresses, a redirect to a private address, a DNS answer that changes between check and connect, and a non-standard port; saving shows "This address isn't reachable from aios".
- [x] URL size, redirect and time limits are enforced from settings.
- [x] A webhook source's secret is shown once and can be rotated; bad signatures, replays and oversized bodies are rejected and nothing is stored.
- [x] A valid delivery Jev finds clean is stored, searchable and fires `webhook.received`; webhook items are never reconciled away.
- [x] With the fake classifier, a flagged delivery is stored as flagged, isn't searchable, fires nothing and answers 422; an unchecked one answers 503 and stores nothing. A flagged web page or email isn't searchable and fires nothing, and one awaiting check becomes searchable once a retry finds it clean.
- [x] An email source with no allowed senders accepts nothing; spoofed or unauthenticated mail is dropped and counted on the sync record.
- [x] Email replies thread together, attachments are ingested within the size cap, and rotating the address cuts off the old one.
- [x] Email is shown as unavailable when the deployment has no inbound mail domain.
- [x] An event trigger filtered to a source fires for that source's documents and not for uploads; uploads still fire `document.ingested` with the Uploads source.

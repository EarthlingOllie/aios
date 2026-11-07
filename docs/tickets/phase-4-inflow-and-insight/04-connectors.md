# 04: Connectors: credentials, allowlist, choosing what syncs, removal

**What to build:** Admins can connect an external system, choose exactly what it syncs, and end the connection cleanly, demonstrated with the test provider.

Connecting runs an OAuth-style flow with a signed state; credentials are stored encrypted in the platform database under their own key and are never visible anywhere else. A providers list says which systems this deployment offers and why any are unavailable. Admins pick channels, folders or repositories from searchable scope options, with items restricted at the source flagged and needing an explicit acknowledgement that everyone in the organisation will be able to search them. Calls reach only each enabled provider's hosts, and switching a provider off stops it at once. A connector whose access fails shows Reconnect. Removing a connector stops it immediately and either deletes its content or keeps it as Uploads documents.

[Spec](../../specs/phase-4-inflow-and-insight.md)

**Blocked by:** [02: Sync engine, file items, source health](02-sync-engine.md), [Phase 1 · 08: Audit log and approval rules](../phase-1-foundation/08-audit-log-and-approval-rules.md)

**Status:** ready-for-agent

- [ ] Only admins can start a connection; bad, expired or mismatched states are refused (400 or 403); one connector per provider is enforced (409).
- [ ] The credentials adversarial test passes: after syncs and tool listings, no secret appears in any API response, captured log, trace, store column or model-facing listing.
- [ ] A final authorisation failure sets the connector to needs-reconnecting, stops its syncs and shows Reconnect; reconnecting resumes from the stored cursor.
- [ ] Picking restricted items without acknowledging is refused (422), and the warning text matches the spec.
- [ ] Saving the scope creates the connector's source, which syncs; content is searchable by every member; narrowing the scope removes the dropped items at the next sync.
- [ ] A call to a host outside the provider's allowed list is refused and recorded on the sync.
- [ ] Removing a provider from the enabled list marks its connectors unavailable at once and keeps their content searchable.
- [ ] Removing a connector with "Delete" leaves no synced items, documents or search entries; with "Keep" it produces Uploads documents (message items as Markdown, grouped as the spec says) that survive.
- [ ] Agent versions that used a removed connector refuse to start with "the <provider> connector was removed".
- [ ] Connecting, scope changes and removal (with the keep-or-delete choice) are recorded in the audit log.

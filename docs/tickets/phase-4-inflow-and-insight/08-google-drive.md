# 08: Google Drive

**What to build:** Organisations can connect Google Drive. Admins pick folders, including shared drives, and everything under them syncs as documents, with Google Docs, Sheets and Slides exported to formats aios can read. Drive's change feed keeps the store current: files moved in are added, files moved out, trashed or deleted are removed, and edited files are re-ingested. Until Google has verified the app, production deployments list Drive as unavailable with the reason. There are no Drive tools.

[Spec](../../specs/phase-4-inflow-and-insight.md)

**Blocked by:** [04: Connectors: credentials, allowlist, choosing what syncs, removal](04-connectors.md)

**Status:** ready-for-agent

- [ ] Recorded responses cover the change feed, moves in and out of chosen folders, trashing, deletion, and an edited Doc being re-exported and re-ingested.
- [ ] Native Google formats are exported and ingested; unsupported and oversized files are listed as skipped on the sync record.
- [ ] Reconciliation removes files no longer under the chosen folders.
- [ ] Shared drives can be chosen and synced.
- [ ] An unverified production deployment lists Drive as unavailable, with the reason, and connecting is not offered.
- [ ] The connector shows its health on the Data page.

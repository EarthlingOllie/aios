# #37 ledger (orchestrator) — head 1bdd9e0
- F8 PARTLY/ACCEPTED: 17cdee5 — FAILED_MESSAGE/UNAVAILABLE_MESSAGE shared by dataStoreNotice and dataStoreSummary; provisioning wording differs on purpose; drift test (red by temporary break); vitest 58.
- F6 REAL/ACCEPTED: ae205ff — chat hint also requires !listError (matches section); e2e red->green; 92/92 e2e.
- F5 REAL/ACCEPTED: 32941df — useDataStoreStatus returns {status,error}; Overview shows 'Couldn't check your data store (...). Trying again…' while retrying; e2e red->green; 92/92. Merge risk: hook signature change (F1/F7 provider). Follow-up: show store error in DocumentsNotices too.
- F3 REAL/ACCEPTED: 51cf805 — listRetrying only for network/502/503/504; permanent errors shown once without 'Trying again'; probe 6->2 requests; e2e red->green; 92/92. Left open: no global 401 -> sign-in redirect (app-wide).
- F2 REAL/ACCEPTED: 04dfc32 — docs undefined while loading + DocumentsLoading spinner in both views (matches crud.tsx pattern); also fixes chat panel; 2 e2e red->green. Full e2e 92p/1 timeout on unrelated delete test under load (3/3 rerun). Watch delete test at integration.
- F4 REAL/ACCEPTED: f3cb6c6 — refresh sequence number (latest ref, like useLoad) + usePoll (optional interval arg, default POLL_MS) instead of setInterval; 2 e2e red->green with paused clock; 93/93. Note: F3's 'ready && listRetrying' poll condition must carry onto usePoll at merge.
- F1+F7 REAL/ACCEPTED: 22f22b4 — DocumentsProvider (context) wraps Shell route; useSharedDocuments(); tab switch keeps uploads/errors; chat->Data in-flight upload appears; 2 e2e red->green; 93/93 (CI=1). Trade-off: status/list fetched on any signed-in page; upload errors follow across views.
- INTEGRATED: 1fe8457 pushed; 7 fixes; 100/100 e2e, vitest 58.

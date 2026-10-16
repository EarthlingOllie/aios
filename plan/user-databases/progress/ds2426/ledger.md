# #24+#26 merge ledger — head 73b2717 (claude/data-stores-24-26)
Resolutions: store revs 0001->0002 documents->0003 data_schema; #26 drop kept, #24 retry removed; ro prepare_threshold carried; search uses store_not_ready; notices.store_busy(); data tools catch DataStoreBusy (+test); describe_data lists aios.documents/chunks (+test).
Backend 384p/1s/1 flaky; frontend e2e 79/79; real-app drive OK.
Findings triage (orchestrator):
- M1 attempts bump on busy: already decided (#24 F3/F4) — accepted residual, no change.
- M2 #26-era stores at 0002 mean data_schema: #26 never deployed anywhere — no change; note in PR.
- M4 unbounded busy retry: deliberate (#24 F3) — no change.
- M3, M5, M6, M7, M8, M9: to dispatch after #34 findings return (concurrency/usage limit).

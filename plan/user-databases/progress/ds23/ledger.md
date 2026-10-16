# #23 finding ledger (orchestrator)
- F1 PARTLY/no change: 0012 NOT NULL breaks a live 0011 release's sign-up insert (reproduced). Unreachable: 0011 never shipped (main at 0010). Requirement: ship #21-#23 in one deploy; say so in #23 PR. Evidence: ds23/f1/repro.txt
- F5 PARTLY/no change: role CONNECTION LIMIT 10 reachable at 4x3 pooled conns (reproduced) but no prod caller uses store_session yet. Carry to #24: budget limit >= 3x store-role processes or lower max_overflow; map "too many connections for role" to 503/retry. Evidence: ds23/f5/
- F6 NOT REAL/no change: deleting/deleted never written. Carry to deletion slice: deleter takes provisioner advisory lock + status filter in _mark_ready.
- F7 NOT REAL/no change: Redis health reached transitively via migrate. Evidence: ds23/f7/
- F9 NOT WORTH IT/no change: first check deliberately avoids paid embed on unready store.
- F10 REAL(latent)/ACCEPTED: ee5fc57 on claude/ds23-f10 — describe_error made public, worker reuses it; existing test pins stored text; 313p/1s. To cherry-pick.
- F8 REAL(minor)/ACCEPTED: 58c281e on claude/ds23-f8 — provision() returns revision, migrate_all skips just-provisioned ids; red->green test; 314p/1s. To cherry-pick.
- F2 PARTLY/ACCEPTED: d3eb988 on claude/ds23-f2 — README local dev runs migrate-all after upgrade (reproduced permanent 409 without it). Gate kept (prod/compose run migrate-all before rollout).
- F4 REAL/ACCEPTED: d69a007 on claude/ds23-f4 — Celery beat (-B) in provisioning worker retries failed / >1h-stuck stores every 10 min; red->green; real beat worker check; 314p/1s. Precedent: first periodic platform job via Celery beat (existing scheduler.py is for user triggers). Needs terraform apply.

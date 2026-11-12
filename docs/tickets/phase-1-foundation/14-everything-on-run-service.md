# 14: Everything on the run service: graceful shutdown, rate limiter, embeddings, run pages read-only, API turn path removed

**What to build:** Every remaining kind of agent work runs on the run service, and the web servers run none. The run page's chat is removed: run pages become read-only, showing the run's trace, events and usage, and no run-page turn path or relay is built. Until phase 2, people talk to agents by mentioning them in chats. A deploy hands unfinished turns back to the queue to resume from their last saved point, and a process whose event loop stalls exits so its work is retried elsewhere. A shared limiter per model keeps calls within the provider's rate limits, and background embedding of the search index runs on the run service. The old turn path, the per-process cap and the old heartbeat columns are removed, and the full-stack core loop runs end to end on the run service.

[Spec](../../specs/phase-1-foundation.md)

**Blocked by:** [06: Unified search across documents, chats and runs](06-unified-search.md), [12: Run service: queue, mentioned turns, handoff, scheduler](12-run-service.md), [13: Workflows node by node; approvals resume there; workflow worker removed](13-workflows-node-by-node.md), [08: Audit log and approval rules](08-audit-log-and-approval-rules.md), [11: Usage ledger in GBP, caps, mention guards](11-usage-caps-and-mention-guards.md)

**Status:** done

- [x] Run pages are read-only, showing the run's trace, events and usage; the run page's chat, its endpoint and its tests are gone.
  (`POST .../agents/{id}/runs/{run_id}/chat`, `ui_stream`'s request parsing and streaming
  response, `load_chat_context(by_hand)` and the agent page's "New run" button are gone; the run
  page shows its conversation (`role="log"`), turns and event log with no composer, a read-only
  note naming how to talk to the agent, and reads the run again while a turn is going. The
  endpoint's own tests (busy 409, blank 422, stop/disconnect, session release) are deleted; the
  tests that used it to give a run a turn now run the turn as the run service does
  (`conftest.run_turn`). Tests: `test_run_pages_are_read_only_and_the_web_server_runs_no_turns`,
  `e2e/run-page.spec.ts`. `POST .../runs` (an empty run) stays in the API, unused by the UI.)
- [x] Full-stack scenario: restarting the run service mid-turn resumes the turn, with no "stopped mid-run" card; a graceful hand-back doesn't count as an attempt.
  (A turn cancelled by its stopping process records nothing and stays underway; the next claim
  carries it on from the last checkpoint (`Continue`), and a resumed turn handed back part way
  carries on too. Tests: `test_a_turn_handed_back_by_a_stopping_process_resumes_from_its_last_checkpoint`
  (attempts 0, no reply posted meanwhile, one succeeded turn), the resume and scripted-scenario
  versions, and `fullstack/core-loop.spec.ts`'s restart story (the `core-loop` scenario takes
  its time over "slowly"; the fullstack run service's grace is 1 second), passed against a local
  native stack through PgBouncer, restarting the run service with SIGTERM.)
- [x] A process whose loop stalls past the no-progress limit exits, and its work is retried elsewhere.
  (The run service's watchdog, `WORKFLOW_RUN_NO_PROGRESS_LIMIT`. Test: a real subprocess whose
  handler blocks the loop exits with status 1, and its item's lapsed lease lets another
  process run it.)
- [x] With a model rate limit configured, calls across two processes stay within it, people waiting in chat are served first, and an unset limit means no pacing.
  (`aios.model_rate_limit`: a Redis token bucket per model (one Lua script per step, Redis's
  clock), `MODEL_RATE_LIMITS` (Terraform `var.model_rate_limits`), on every agent's model calls
  (middleware) and the handoff's; waiters ordered by lane then arrival. Tests: two subprocesses
  at 6/minute get 6 between them; tokens spent hold the next call; a mention beats earlier
  schedules; an unset model touches no Redis key; a mention's handoff and turn are paced as
  chat work.)
- [x] Unembedded search items are embedded by background sweeps that never take capacity reserved for chat.
  (Work kind `embedding_sweep` (platform 0072), background lane, one per store, enqueued by every
  process each `SEARCH_EMBED_INTERVAL_SECONDS`; the Celery `embed_pending_search` /
  `embed_search_items` tasks and beat entry are gone. Tests: an item is embedded by a drained
  sweep; with 3 slots and 2 reserved one sweep runs at a time and a mention still starts; an
  exhausted sweep is dropped quietly.)
- [x] Web servers run no agent turns, and the per-process concurrent-runs setting and the old heartbeat columns are gone.
  (No route posts to a run; the per-process cap was already gone (only `RUN_SERVICE_CONCURRENCY`).
  Store 0022 drops `agent_runs.heartbeat_at` (and its index), `invocations.heartbeat_at` and
  `workflow_runs.heartbeat_at`, and renames the schedule claim's `triggers.heartbeat_at` to
  `claimed_at`. Test: `test_the_old_heartbeat_columns_are_gone`.)
- [x] The full-stack core loop (mention, schema change, approval, resume, reply) runs on the run service through the pooler, writes one usage row per model call, needs an editor to approve, and appears in the audit log.
  (`fullstack/core-loop.spec.ts` now also reads the audit log: one `approval.decided`, approved,
  `needs_role: editor`, by the person. Passed against a local native stack (API, run service,
  ingest/urgent/routine workers, `DATA_STORE_POOLER_ADDRESS` on PgBouncer); not run on Compose
  or CI here.)

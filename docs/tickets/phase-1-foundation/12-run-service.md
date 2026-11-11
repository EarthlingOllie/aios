# 12: Run service: queue, mentioned turns, handoff, scheduler

**What to build:** A new run service takes agent work out of the web servers. A work queue in the platform database serves people waiting in chat before background work, and the organisation with the least in flight first, under a soft per-organisation cap that admins can override. Mentioned agents' turns, the handoff before every mention, and the scheduler all run on it. Each run service process runs many turns at once, with leases, retries and a limit on attempts, and a "drain the queue once" entry point lets tests run it deterministically.

[Spec](../../specs/phase-1-foundation.md)

**Blocked by:** [01: Connection pooler and turn lease](01-pooler-and-turn-lease.md), [10: An invocation for every run, and turns as records](10-invocations-and-turns.md)

**Status:** done

- [x] Starvation test: while one organisation floods the queue, another organisation's mentioned turn starts within the target time.
  (`work_items` in the platform database (revision 0066); claiming in one transaction: per
  organisation in-flight counts, each organisation's ready items ranked and cut to its cap,
  candidates ordered interactive first, fewest in flight, oldest, then locked with SKIP LOCKED
  and leased (`aios.run_service.queue.claim`). `test_run_service.py`: 1,000 background items
  from one organisation, then another's interactive item starts well within
  `QUEUE_CHAT_START_TARGET_SECONDS` (5).)
- [x] An item whose lease expires is retried elsewhere, and after three attempts its invocation fails with "The service stopped mid-run."
  (Leases of `QUEUE_LEASE_SECONDS` (90), extended every 30 s; each claim counts an attempt and an
  item claimed past `QUEUE_MAX_ATTEMPTS` (3) fails its invocation, its running turn and its
  schedule's firing (`aios.run_service.work.fail`). Handlers are idempotent: a retried agent turn
  carries on from the run's last checkpoint (`aios.api.agents.Continue`) without repeating the
  message. Tests: lapsed lease retried; a turn whose process hangs mid-call is carried on by a
  second process; three dead attempts fail a mention's and a schedule's invocation.)
- [x] An organisation at its in-flight cap waits while other organisations' work is claimed; each process keeps reserved capacity for people waiting in chat.
  (`QUEUE_IN_FLIGHT_CAP` (20, soft); `RUN_SERVICE_CONCURRENCY` (50) with
  `RUN_SERVICE_INTERACTIVE_RESERVE` (10) never taken by background work
  (`aios.run_service.executor.Executor.room`).)
- [x] A lost enqueue is repaired by the requeue sweep.
  (Enqueue after the store commit, idempotent by deduplication key; `aios.run_service.sweep`
  re-enqueues mentions' and schedules' invocations left unstarted without an item for over
  `QUEUE_REQUEUE_AFTER_SECONDS` (60), every 30 s on each process. Replaces failing them as
  "never started" on chat reads.)
- [x] An admin raises their organisation's in-flight cap, and more of its work is then claimed at once; editors and participants can't change it.
  (`organisations.in_flight_cap`; `PUT /organisation/in-flight-cap` (admins, audited as
  `settings.changed`); `GET /organisation` returns `in_flight_cap` and
  `in_flight_cap_overridden`. Added to the permission table. No UI control yet.)
- [x] Agent and workflow mentions are briefed on the run service, and a failed handoff fails its invocation as today.
  (Posting enqueues an invocation start (interactive lane) after the store commit; the start runs
  the handoff, then enqueues the agent's turn or starts the workflow run for the workflow worker.
  The firing machinery (`aios.firing`), its per-process cap and the heartbeat sweeps for chat
  turns are removed; the chat tests run through the drain-once entry point via the suite's
  `run_service` fixture.)
- [x] Each run of a schedule fires exactly once across run service processes, the scheduler-enabled setting applies to them, and missed runs aren't caught up.
  (The scheduler ticks on each run service process when `SCHEDULER_ENABLED`, and no longer in the
  API's lifespan; a tick claims due rows, starts the agent run and its invocation, and enqueues
  the turn in the background lane. Tests: four concurrent ticks fire one run; the run service
  ticks only when enabled; the API runs none.)
- [x] Compose and the AWS deployment run the run service.
  (Compose `run-service` and `fullstack-run-service` (`python -m aios.run_service`); Terraform
  `aws_ecs_task_definition.run_service`, service and log group, sized by `var.run_service`.
  `docker compose config` checked; `terraform validate`/`plan` not run here (no terraform
  binary, and nothing applied). The full-stack core loop passed against a local stack with the
  run service as a process.)

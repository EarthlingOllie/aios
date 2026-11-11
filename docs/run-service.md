# Run service

The **run service** (`aios.run_service`) runs agent work outside the web servers: mentioned
agents' turns, the handoff before every mention (see [Chats](chats.md#the-handoff-brief)),
workflow runs node by node (see [Workflows](workflows.md#runs)), resuming runs once a schema
change they asked for is decided, and the schedules (see [Triggers](triggers.md)). It's its own service on the API image
(`python -m aios.run_service`; Compose's `run-service`, ECS's `run-service`), and each of its
processes runs many items at once in one event loop. A run's page still runs its own chat turns
in the API process.

## The queue

Work waits in a queue in the platform database (`work_items`, `aios.run_service.queue`). Each
item records its organisation and store, its **lane** (`interactive`: people waiting in a chat,
i.e. mentions; `background`: schedules, workflow advances and nodes, resumes), its **kind** (an
invocation start, an agent turn, a workflow advance, a workflow node, or a resume turn), a
payload of store ids, when it became ready and was enqueued, its attempts and lease, and an
optional deduplication key. Items are deleted when done: their outcome lives in the store.

- **Enqueueing** happens after the store commits, and is idempotent by the deduplication key
  (one start and one turn per invocation, one advance per workflow run, one item per workflow
  step, one resume per agent run). An advance or resume enqueued while its key's item is running
  marks that item to run once more when it's done (`work_items.again`), so it sees what changed. Processes are woken through a Redis channel per lane,
  with a poll every `RUN_SERVICE_IDLE_POLL_SECONDS` (5) as a fallback.
- **The requeue sweep** (every 30 seconds, on every process) re-enqueues invocations left
  unstarted without an item for over `QUEUE_REQUEUE_AFTER_SECONDS` (60): a mention's from
  posting until its run starts, a schedule's agent turn until it begins. So an enqueue lost
  between the store's commit and the queue repairs itself.
- **Claiming** is one transaction in two stages. First it chooses candidates: it counts each
  organisation's items in flight, ranks each organisation's ready items (chat first, then by
  readiness), keeps only as many as its cap leaves room for, and orders them interactive lane
  first, then by fewest in flight, then oldest. Then it locks and leases those still free,
  skipping locked rows.
- **The in-flight cap** is `QUEUE_IN_FLIGHT_CAP` (20) per organisation, which an admin can
  override for their organisation (`PUT /api/o/{org}/organisation/in-flight-cap`, `null` for
  the default; audited as `settings.changed`); the organisation's page returns it
  (`in_flight_cap`, `in_flight_cap_overridden`). It's soft: processes claiming at once can each
  fill an organisation's room, so it can overshoot by the few items each claims, which was
  accepted over serialising claims.
- **Each process** runs up to `RUN_SERVICE_CONCURRENCY` (50) items, and keeps
  `RUN_SERVICE_INTERACTIVE_RESERVE` (10) of those slots for the interactive lane: background
  work never takes them.

## Leases, retries and attempts

A claimed item is leased for `QUEUE_LEASE_SECONDS` (90), extended every third of that while it
runs. A process that dies lets its leases lapse, and the item is claimed again elsewhere; each
claim counts an attempt. An item claimed more than `QUEUE_MAX_ATTEMPTS` (3) times fails its
invocation (and its schedule's firing) with "The service stopped mid-run."

Every kind of work can run again for the same item, carrying on from where its invocation got
to (`aios.run_service.work`):

| Kind | What it does |
| --- | --- |
| Invocation start | A mention's: runs the handoff, then enqueues the agent's first turn, or starts the workflow's run and enqueues its first advance. A handoff already done isn't asked again; one that fails fails the invocation. |
| Workflow advance | Moves a workflow run on under its row lock: rebuilds its state from its recorded steps, checks its active time against its limit, starts the nodes now ready (expanding subworkflows) and enqueues their items, resumes a waiting node whose request has been decided or has expired, and ends the run once nothing is ready or running. Its attempts running out fail the run. |
| Workflow node | One agent node's turn, as a child invocation of the run's, then an advance. A turn an earlier attempt began carries on from its last checkpoint. Its attempts running out fail its step, and then the run. |
| Resume turn | An agent run waiting on schema change requests, once one is decided or has expired: its waiting tool calls get their outcomes and the turn goes on. |
| Agent turn | Starts the mentioned agent's run, if it hasn't one yet, and runs one turn of it under the run's turn lease, through the chat endpoint's turn code. A schedule's run was started when it fired. A turn an earlier attempt began carries on from the run's last checkpoint without giving the run its message again. The reply, or why it failed, goes back to the chat, or onto the schedule. |

A turn whose run is held by another turn (past `TURN_WAIT_SECONDS`) is handed back to run again
a few seconds later, without counting an attempt; so is an item a stopping process hands back.
A handler that raises unexpectedly is handed back after ten seconds, counting the attempt.

## The scheduler

With `SCHEDULER_ENABLED` (the default), every process ticks the scheduler every
`SCHEDULER_INTERVAL_SECONDS` (15): it claims due schedules from the due index, skipping locked
rows, moves them on, starts each one's run and invocation in the store, and enqueues the agent's
turn in the background lane (a workflow's run gets its first advance there). Each run of a
schedule fires exactly once across processes, and missed runs aren't caught up. See
[Triggers](triggers.md#the-scheduler).

## Stopping

On a termination signal a process stops claiming, lets its items finish for up to
`RUN_SERVICE_SHUTDOWN_GRACE_SECONDS` (25), then cancels the rest and hands them back to the
queue without counting the attempt. Compose and ECS give it 30 seconds before killing it.

## In tests

`aios.run_service.executor.drain_once` runs every ready item to completion in-process, and what
they enqueue. The backend suite's `run_service` fixture drains once after each request that
wakes the run service (as if a process were always up); a test that wants to watch the queue
turns that off (`run_service.auto = False`) and drains when it chooses.

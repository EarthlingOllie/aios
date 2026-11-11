# Triggers

**Triggers** start agents and workflows without a user in the loop. Each has a kind, and the
first is a **schedule**: a cron expression (5 fields, or `@hourly`, `@daily`, `@weekly`,
`@monthly`, `@yearly`) in an IANA timezone. The **Scheduler** page lists every schedule, the
next to run first, and creates, edits, pauses and deletes them; each agent's and workflow's page
does the same for its own (`/api/triggers`). Schedules belong to the organisation (see
[Accounts and organisations](accounts-and-organisations.md#what-the-organisation-owns)), and a
schedule's run records its schedule rather than a member.

## Schedules

- An agent's schedule carries a message. Each time it fires, a new run of the agent's *current*
  version starts with that message as the user's, and runs one turn exactly as a chat turn does
  (the run is listed with the agent's others, marked as scheduled). The schedule shows how its
  last run went, with a link to it.
- A workflow's schedule carries an input. Each time it fires, a run of the workflow's *current*
  version starts with it, exactly as starting one by hand does (refused the same way, which the
  schedule records as a failure), and is queued for the worker. The schedule shows how that run
  is going, then how it went, with a link to it.
- Missed runs aren't caught up: a schedule that's overdue (nothing was running) fires once, then
  carries on from now. Across daylight-saving changes it behaves like cron: a skipped local time
  runs when the clock jumps, and a repeated one runs once.
- Pausing or editing a schedule that has just been claimed, before its run has started, drops
  that firing (it was for the old settings); the trigger row is locked while the run is created,
  so an edit racing it either drops it or waits and lets the started run finish.
- Deleting an agent or a workflow deletes its schedules. Archiving an agent pauses them, and
  unarchiving leaves them paused; a schedule firing for an archived agent fails with "The agent
  <name> is archived.". Cron parsing is `cronsim`'s, wrapped in
  `aios.cron`.

## The scheduler

- Each API process runs the scheduler (`aios.scheduler`), polling every
  `SCHEDULER_INTERVAL_SECONDS` (15). Due schedules are claimed with `FOR UPDATE SKIP LOCKED`
  and moved on to their next run in the same transaction, so with any number of replicas each
  run fires once. Set `SCHEDULER_ENABLED=false` to keep a process from running them (or chat
  turns: see below).
- A process fires at most `SCHEDULER_MAX_CONCURRENT_RUNS` (5) schedules at once (an agent's turn
  runs in the API process, next to the chats it serves), and claims no more than it has free
  slots for. What it can't start stays due, for another replica or its next poll, so a burst
  (many schedules sharing `0 9 * * *`) runs a little late rather than crowding out chat.
- A turn still running when the process stops keeps what it got to, and its schedule records
  that it failed. A process that dies can't record that, so while a schedule is running the
  process firing it refreshes a heartbeat (every 30s), and any replica's tick fails a running
  schedule whose heartbeat has stopped for 90s; replicas that are up keep beating their own, so
  they're left be.

## Firing

The firing itself is shared machinery (`aios.firing`), for whatever causes a run outside a
request: a schedule, and a mention of an agent in a chat (`aios.api.chats.TurnFiring`; see
[Chats](chats.md#mentioning-an-agent)), which share its cap. It holds the cap, the heartbeat,
the stall sweep, and "start a run and run one turn of it"; what's the cause's own (what it
claims and how it starts the run, where its heartbeat and outcome live) is a small object the
caller supplies (an `aios.firing.Cause`). While a fire runs, the process keeps a heartbeat on
the run itself (`agent_runs.heartbeat_at`) as well as the cause's, so a run whose process died
is failed on its own page too, and not only on its schedule or its chat card.

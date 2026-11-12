# Triggers

**Triggers** start agents and workflows without a user in the loop. Each has a kind, and the
first is a **schedule**: a cron expression (5 fields, or `@hourly`, `@daily`, `@weekly`,
`@monthly`, `@yearly`) in an IANA timezone. The **Scheduler** page lists every schedule, the
next to run first, and creates, edits, pauses and deletes them; each agent's and workflow's page
does the same for its own (`/api/o/{org}/triggers`). Schedules belong to the organisation (see
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
- Archiving an agent or a workflow pauses its schedules, and unarchiving leaves them paused; a
  schedule firing for an archived one fails with "The agent <name> is archived." (or "The
  workflow <name> is archived."). A schedule for an archived agent or workflow can only be made
  or changed while paused: turning one on answers 409 with the same. Cron parsing is
  `cronsim`'s, wrapped in `aios.cron`.

## The scheduler

- Schedules are in each organisation's store (`aios.triggers`). The platform keeps a thin index
  of what's due (`schedule_due`: store, trigger, organisation and next run time; `aios.schedules`),
  written whenever a schedule is saved (created, edited, paused, archived with its target,
  deleted), once the store has committed it. The repair sweep (the routine worker's
  `aios.repair_stores`, with the people mirror's) rebuilds it from every ready store's triggers
  and logs what it fixed: rows added (lost), updated (stale) and removed.
- The scheduler runs on the run service (`aios.scheduler`, see [Run service](run-service.md)):
  each of its processes polls every `SCHEDULER_INTERVAL_SECONDS` (15), unless
  `SCHEDULER_ENABLED=false`. Due index rows are claimed with `FOR UPDATE SKIP LOCKED`; for each,
  the trigger is re-read in its store under its row lock (skipped, and left due, while an edit
  or a starting firing holds it) and moved on to its next run there, then its row moves with
  it, so with any number of processes each run fires once. A row that's out of date fires
  nothing: the trigger decides, and the row is put right.
- Firing an agent's schedule starts its run and invocation in the store and enqueues the turn
  in the run service's background lane, which people waiting in chat come before, under the
  organisation's in-flight cap. The turn's outcome goes on the schedule. A turn whose process
  dies is retried on another, and after three attempts the schedule records "The service
  stopped mid-run."
- A process that dies between claiming a schedule and starting its run can't record that, so
  any process's tick fails a schedule claimed over 90 seconds ago that never started a run.


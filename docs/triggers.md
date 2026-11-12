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

## Event triggers

An **event trigger** fires when something happens in the organisation's store, recorded in its
outbox (`aios.outbox`) and fired by the dispatcher (`aios.dispatcher`) under the loop rules (its
own target's events, repeats in a chain of causes, maximum depth, rate). Its filter fields, each
a list any of whose values matches (`filter` on the API; an unknown field is a 422):

| Event | Fires when | Filter |
| --- | --- | --- |
| Document ingested | a document becomes ready | `sources`, `content_types` (`text/*`) |
| Table written | `write_data` commits | `tables` (with or without `data.`), `operations` |
| Schema change applied | an approved schema change is applied | `tables` |
| Chat message | any message is posted in any chat | `chat_id` (required), `author_kinds`, `mentions` (`agent:3`), `keywords` |
| Webhook received | its own endpoint accepts a call | none |

- **Keywords** match whole words or phrases, ignoring case: `outage` doesn't match `outages`.
- **Any chat.** A chat message trigger may listen to any chat of the organisation, and deliver
  to any, whether or not its creator is in either. Saving it adds its agent or workflow to the
  chat it listens to, as delivery does. Its own posts there are its own events, so they don't
  fire it again (unless `fire_on_own_events`); likewise an agent's own table writes.
- **Debounce.** With `debounce_seconds`, matches gather into one batch per trigger
  (`aios.event_batches`) that fires once when the window closes: the run is given the events
  (at most `EVENT_BATCH_CAP`, 500) and how many more were dropped. A workflow's template can use
  `{{count}}`, `{{dropped}}` and `{{events}}`. The store is stamped to be claimed again when its
  next batch closes.
- **Webhooks.** A webhook trigger answers its create with its URL and signing secret; the secret
  is shown only then, and when `POST .../triggers/{id}/webhook-secret` rotates it (which changes
  the URL too). Callers `POST` JSON to `/api/webhooks/{token}` with `X-Aios-Timestamp` (Unix
  seconds, within `WEBHOOK_TOLERANCE_SECONDS`, 300) and `X-Aios-Signature: sha256=<hex>`, the
  HMAC-SHA256 of `<timestamp>.<raw body>` keyed by the secret, and optionally
  `X-Aios-Delivery`, an id de-duplicated for `WEBHOOK_DEDUPE_HOURS` (24). The endpoint answers
  404 (unknown or disabled token), 429 (past `WEBHOOK_RATE`, 120 a minute per token), 415 (not
  JSON), 413 (past `WEBHOOK_BODY_LIMIT_BYTES`, 1 MiB), 401 (bad signature or timestamp), 202
  for a repeated delivery id (nothing written), 422 for a body that doesn't fit the target
  workflow's input schema, then Jev's check: 422 if flagged (audited as `webhook.flagged`), 503
  with `Retry-After` if Jev can't check it. Neither records the delivery id, so a retry is
  processed. Accepted, the event is written and the answer is 202 with the delivery id; the
  dispatcher fires the trigger shortly after. A workflow's input is the body (or the trigger's
  template, rendered with `{{payload.body...}}`). Only the token's hash is kept, in the store
  and in the platform's `webhook_tokens` index; the secret is encrypted; both are on the store's
  deny list.

## Delivering results to a chat

A trigger can name a chat its runs' results are delivered to (**Deliver to** on the schedule
forms; `deliver_to` on the API): any chat of the organisation that isn't archived, whether or not
whoever sets it is in it. Choosing one adds the trigger's agent or workflow to the chat, and the
trigger records who chose it. An archived chat is refused (409) with why.

- Nothing is posted when the run starts. When it ends, a successful run's result is summarised by
  the handback (see [Chats](chats.md#handback)) and posted once, from the agent or workflow, with
  the run's card collapsed under it and the full output behind it; a failed one posts a short
  failure message, and the trigger's creator and the organisation's admins are notified.
- If the chat has been archived since it was chosen, the run still runs, but nothing is
  delivered: the trigger shows why (`delivery_problem`), with a prompt to choose another chat,
  and whoever chose it is notified. Choosing another chat clears it.
- The chat is fixed for each run as it fires (`invocations.delivery_chat_id`), and why nothing was
  delivered is kept on the run's invocation (`delivery_error`).

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


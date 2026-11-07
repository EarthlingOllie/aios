# 11: Event triggers: outbox, dispatcher, loop prevention, document ingested

**What to build:** Runs can start when something happens in the store, starting with a document being ingested.

- **Events.** Changes write events in the same transaction, each carrying its chain of causes.
- **Dispatcher.** It matches events against event triggers and starts runs through the run service.
- **Loop rules.** They stop automation feeding itself: no firing on a trigger's own target's events unless allowed, no repeating a trigger within one chain, a maximum depth, and a rate cap per trigger.

Editors and admins create document-ingested triggers, with filters, on the trigger forms.

[Spec](../../specs/phase-3-agents-and-automation.md)

**Blocked by:** [Phase 1 · 10: An invocation for every run, and turns as records](../phase-1-foundation/10-invocations-and-turns.md), [Phase 1 · 12: Run service: queue, mentioned turns, handoff, scheduler](../phase-1-foundation/12-run-service.md)

**Status:** ready-for-agent

- [ ] Ingest, `write_data`, applied schema changes and chat messages each write their event in the same transaction as the change. A rolled-back change writes none.
- [ ] A pending stamp and a wake-up bring the dispatcher to a store with events. A sweep recovers a lost stamp. Dispatched events are pruned after the retention period.
- [ ] The dispatcher has a "drain once" entry point that tests use.
- [ ] A ready document fires each matching document-ingested trigger once, with source and content-type filters.
- [ ] An agent target gets a one-turn run on its current version. A workflow target gets an input from its template, or the event, validated against its input schema.
- [ ] The loop rules apply in order: own events, no repeat in the chain, maximum depth, rate cap. A two-trigger cycle is cut at its second pass.
- [ ] Each refusal is recorded on the trigger's last result and in the audit log. A trigger over its rate shows "Rate limited".
- [ ] Only editors and admins create or edit triggers.

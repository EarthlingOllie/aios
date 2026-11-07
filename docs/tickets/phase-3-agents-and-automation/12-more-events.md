# 12: More events: table written, schema applied, chat message, debounce, webhooks

**What to build:** Event triggers cover the rest of the store and the outside world.

- **Table written and schema applied:** filtered by table and operation.
- **Chat message:** filtered by author kind, mentions and whole-word keywords. A trigger can name a direct message only if its creator is in it.
- **Debounce:** a burst gathers into one run.
- **Webhooks:** each trigger gets its own signed public endpoint.

[Spec](../../specs/phase-3-agents-and-automation.md)

**Blocked by:** [06: Structured workflows: typed I/O, templates, conditional edges](06-structured-workflows.md), [11: Event triggers: outbox, dispatcher, loop prevention, document ingested](11-event-triggers.md)

**Status:** ready-for-agent

- [ ] Table-written and schema-applied triggers filter by table and operation. An agent's own writes don't re-trigger it.
- [ ] The full-stack test covers a table-written trigger firing once, and its echo being refused.
- [ ] Chat-message keywords match whole words, ignoring case.
- [ ] Naming a direct message the creator isn't in is refused (403). The creator leaving disables the trigger and shows why. The same rule covers the trigger's delivery chat.
- [ ] With a debounce window, thirty matches give one run. Batches are capped, with a dropped count.
- [ ] A webhook trigger returns its URL and secret on create (the secret only once), and the secret can be rotated.
- [ ] The endpoint answers 401 for a bad signature or stale timestamp, and 404 for an unknown or disabled token. A repeated delivery id is accepted (202) without writing an event.
- [ ] The endpoint answers 413, 415 and 429 for size, content type and rate. Accepted calls get 202, and dispatch happens asynchronously.
- [ ] Webhook payloads are validated against the target workflow's input schema.

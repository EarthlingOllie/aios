# 12: More events: table written, schema applied, chat message, debounce, webhooks

**What to build:** Event triggers cover the rest of the store and the outside world.

- **Table written and schema applied:** filtered by table and operation.
- **Chat message:** filtered by author kind, mentions and whole-word keywords. A trigger can listen to any chat, and deliver to any chat. Listening to a chat adds the trigger's agent or workflow to it, as delivery does.
- **Debounce:** a burst gathers into one run.
- **Webhooks:** each trigger gets its own signed public endpoint. Jev checks every body before anything fires.

[Spec](../../specs/phase-3-agents-and-automation.md)

**Blocked by:** [06: Structured workflows: typed I/O, templates, conditional edges](06-structured-workflows.md), [11: Event triggers: outbox, dispatcher, loop prevention, document ingested](11-event-triggers.md), [Phase 2 · 08: Handback, and delivery to chats](../phase-2-organisation-experience/08-handback-and-delivery.md), [Phase 2 · 09: post_message tool](../phase-2-organisation-experience/09-post-message.md), [Phase 1 · 15: Jev: classify untrusted content](../phase-1-foundation/15-jev-classification.md)

**Status:** ready-for-agent

- [ ] Table-written and schema-applied triggers filter by table and operation. An agent's own writes don't re-trigger it.
- [ ] The full-stack test covers a table-written trigger firing once, and its echo being refused.
- [ ] Chat-message keywords match whole words, ignoring case.
- [ ] A trigger can listen to, and deliver to, a chat its creator isn't in. Saving a chat-message trigger adds its target to the chat it listens to.
- [ ] With a debounce window, thirty matches give one run. Batches are capped, with a dropped count.
- [ ] A webhook trigger returns its URL and secret on create (the secret only once), and the secret can be rotated.
- [ ] The endpoint answers 401 for a bad signature or stale timestamp, and 404 for an unknown or disabled token. A repeated delivery id is accepted (202) without writing an event.
- [ ] The endpoint answers 413, 415 and 429 for size, content type and rate. Accepted calls get 202, and dispatch happens asynchronously.
- [ ] A body Jev flags answers 422 and is audited; one Jev can't check answers 503 with a retry-after. Either way no event is written and the delivery id isn't recorded, so a retry is processed.
- [ ] Webhook payloads are validated against the target workflow's input schema.

# 01: Open chats, and the Jev check on posted messages

**What to build:** Every conversation is an open chat. Anyone in the organisation can read any chat without joining, and posting joins you to it. Each chat includes the agents and workflows chosen for it, and only those can be mentioned there. Every message a person posts is checked by Jev before it's stored.

- **Reading.** The chat list shows every chat in the organisation, joined or not. Anyone in the organisation can read a chat's messages, chat members and run cards.
- **Posting joins.** Posting in a chat you haven't joined joins you, in the same transaction as the message.
- **Included agents and workflows.** The agents and workflows among a chat's chat members are the ones it includes. A message mentioning one the chat doesn't include is refused, naming it, and the screen offers **Add it to this chat**, which adds it and sends the message.
- **The Jev check.** In the posting path, after the permission, archived and mention checks and before anything is written, the message goes through phase 1's `classify(content, source)` with the source "chat message".
  - A flagged message is refused with the reason and nothing is written: no message, invocation, search item, notification or stream event. The screen keeps the text in the composer.
  - If Jev errors or doesn't answer within the timeout (a few seconds), the message is refused with "couldn't check this message, try again", and nothing is written.
  - A passed message is stored with its verdict, copied onto its search item.
  - People's messages are wrapped in an untrusted block wherever they enter model context (the mentioning message, the chat history the handoff reads, and the brief), whatever the verdict.
  - Posts by agents and workflows aren't checked here.
- **Old columns go.** The chats' visibility and the chat members' role are dropped in this change, with their enum types. Nothing is migrated: dev data is rebuilt with `make seed`.

[Spec](../../specs/phase-2-organisation-experience.md)

**Blocked by:** [Phase 1 · 05: Triggers, chats, invocations and schema change requests move](../phase-1-foundation/05-move-triggers-chats-approvals.md), [Phase 1 · 10: An invocation for every run, and turns as records](../phase-1-foundation/10-invocations-and-turns.md), [Phase 1 · 15: Jev: classify untrusted content](../phase-1-foundation/15-jev-classification.md)

**Status:** ready-for-agent

- [ ] A member who hasn't joined a chat can list it and read its messages, chat members and run cards.
- [ ] Posting in a chat you haven't joined joins you, and the chat then counts unread for you.
- [ ] A message mentioning an agent or workflow the chat doesn't include gets a 422 naming it, and nothing is stored. **Add it to this chat** adds it and sends the message.
- [ ] With Jev's fake flagging a message, the post gets a 422 with the reason, and nothing is stored, indexed, notified or streamed.
- [ ] With Jev's fake failing or slower than the timeout, the post gets a 503 saying to try again, and nothing is stored.
- [ ] A passed message carries its verdict, and so does its search item.
- [ ] With the scripted model, a mentioned agent receives people's messages inside untrusted blocks.
- [ ] Two chats can't share a name (409).
- [ ] No code reads visibility or the chat member role, and the columns and their types are gone.
- [ ] Browser tests cover reading without joining, **Add it to this chat**, and a refused message kept in the composer.

# 01: Visibility check for conversations; private chats become direct messages

**What to build:** Every way of reaching a conversation's content goes through one shared visibility check: the conversation's own reads, run pages and run lists (through each run's invocation), and search. Conversations then gain a kind, chat or direct message. A store migration turns today's private chats into direct messages and deletes private chats with no people left in them. Once this lands, a direct message's content can't be read by anyone outside it, admins included. That closes today's leak through run pages. The conversation list shows Chats and Direct messages as two groups.

- A direct message's name is optional and needn't be unique. A chat's name stays required and unique among the organisation's chats.
- The old visibility and chat member role columns stay for now, so the previous release keeps working. Ticket 11 drops them.

[Spec](../../specs/phase-2-organisation-experience.md)

**Blocked by:** [Phase 1 · 05: Triggers, chats, invocations and schema change requests move](../phase-1-foundation/05-move-triggers-chats-approvals.md), [Phase 1 · 10: An invocation for every run, and turns as records](../phase-1-foundation/10-invocations-and-turns.md)

**Status:** ready-for-agent

- [ ] Runs and workflow runs started in a direct message are a 404 on their pages, and missing from run lists, for anyone not in that direct message, admins included.
- [ ] Search leaves a direct message's content out for anyone outside it.
- [ ] Counts that include those runs (tokens, cost, success) still count them, without exposing content.
- [ ] The migration, run through the store migration command on a store seeded with today's shapes:
  - [ ] makes public chats chats;
  - [ ] makes private chats with people direct messages, keeping their names and chat members;
  - [ ] deletes private chats with no people.
- [ ] Two chats can't share a name (409); two direct messages can.
- [ ] The conversation list returns each conversation's kind, and the Chats section groups by it.
- [ ] Code reads the kind, not the visibility. The visibility and role columns still exist.
- [ ] The release note says private chats became direct messages.

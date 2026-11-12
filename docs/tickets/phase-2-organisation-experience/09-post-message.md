# 09: post_message tool

**What to build:** Editors can give agents a tool for posting into chats they're chat members of, so agents can report where people will see it. The decided rules apply:

- No turn posts into the chat it's answering, into an archived chat, or into a chat the agent isn't a chat member of.
- Each turn has a cap on how many posts it can make.

Posts come from the agent, belong to the turn's invocation, and appear live on the stream. They're derived text: stored with the verdict `derived`, never re-classified by Jev, and wrapped as untrusted wherever they enter model context. People mentioned in a post are notified; agents and workflows mentioned start nothing.

[Spec](../../specs/phase-2-organisation-experience.md)

**Blocked by:** [02: Chats without owners, and archiving chats](02-chats-without-owners.md), [07: Notifications across organisations: mentions, replies, failures](07-notifications.md), [Phase 1 · 11: Usage ledger in GBP, caps, mention guards](../phase-1-foundation/11-usage-caps-and-mention-guards.md)

**Status:** done

- [x] With the scripted model, a turn started in a chat posts into another chat the agent is in, and the post appears on that chat's stream, from the agent.
- [x] Posting into the chat being answered, into an archived chat, or into one the agent isn't a chat member of is refused, with a clear reason.
- [x] The per-turn cap refuses posts beyond it.
- [x] People mentioned in a post are notified. Agents and workflows mentioned start nothing.
- [x] Each post is recorded in the usage ledger as a tool call.
- [x] A post carries the verdict `derived`, and a later turn receives it inside an untrusted block.

# 09: post_message tool

**What to build:** Editors can give agents a tool for posting into chats they're chat members of, so agents can report where people will see it. The decided rules apply:

- No turn posts into the chat it's answering, into an archived chat, or into a chat the agent isn't a chat member of.
- Each turn has a cap on how many posts it can make.

Posts come from the agent, belong to the turn's invocation, and appear live on the stream. People mentioned in a post are notified; agents and workflows mentioned start nothing.

[Spec](../../specs/phase-2-organisation-experience.md)

**Blocked by:** [02: Chats without owners, and archiving chats](02-chats-without-owners.md), [07: Notifications across organisations: mentions, replies, failures](07-notifications.md)

**Status:** ready-for-agent

- [ ] With the scripted model, a turn started in a chat posts into another chat the agent is in, and the post appears on that chat's stream, from the agent.
- [ ] Posting into the chat being answered, into an archived chat, or into one the agent isn't a chat member of is refused, with a clear reason.
- [ ] The per-turn cap refuses posts beyond it.
- [ ] People mentioned in a post are notified. Agents and workflows mentioned start nothing.
- [ ] Each post is recorded in the usage ledger as a tool call.

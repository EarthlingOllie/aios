# 09: post_message tool

**What to build:** Editors can give agents a tool for posting into conversations they're chat members of, so agents can report where people will see it. The decided rules apply:

- A turn started in a chat can post only into chats.
- A turn started in a direct message can post only into that direct message. So direct messages never receive posts from agents working elsewhere.
- No turn posts into the conversation it's answering, or into an archived conversation.
- Each turn has a cap on how many posts it can make.

Posts come from the agent, belong to the turn's invocation, and appear live on the stream. People mentioned in a post are notified; agents and workflows mentioned start nothing.

[Spec](../../specs/phase-2-organisation-experience.md)

**Blocked by:** [03: Direct messages, and copying one into a chat](03-direct-messages.md), [07: Notifications across organisations: mentions, replies, failures](07-notifications.md)

**Status:** ready-for-agent

- [ ] With the scripted model, a turn started in a chat posts into another chat the agent is in, and the post appears on that chat's stream, from the agent.
- [ ] A turn started in a chat is refused when posting into a direct message. A turn started in a direct message is refused when posting anywhere else.
- [ ] Posting into the conversation being answered, into an archived conversation, or into one the agent isn't a chat member of is refused, with a clear reason.
- [ ] The per-turn cap refuses posts beyond it.
- [ ] People mentioned in a post are notified if they can see the conversation. Agents and workflows mentioned start nothing.
- [ ] Each post is recorded in the usage ledger as a tool call.

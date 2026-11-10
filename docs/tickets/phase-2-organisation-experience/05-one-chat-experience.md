# 05: One chat experience: "Chat with this agent"

**What to build:** Talking to a single agent happens in an open chat, like every other conversation. (Phase 1 · 14 already made run pages read-only and removed their chat; until this ticket, people talk to an agent by mentioning it in a chat.)

- **Chat with this agent.** A button on the agent's page makes a new open chat that includes that agent (and not the default agents), with the person in it, named "<Agent> – <person> – <date>". A clash adds " (2)", " (3)" and so on. Any chat member can rename it. The chat shows a short notice that the whole organisation can see it. It respects **invocable by**.

[Spec](../../specs/phase-2-organisation-experience.md)

**Blocked by:** [02: Chats without owners, and archiving chats](02-chats-without-owners.md), [04: Streamed conversations and chat list, with limits and fallback](04-streaming.md), [Phase 1 · 11: Usage ledger in GBP, caps, mention guards](../phase-1-foundation/11-usage-caps-and-mention-guards.md)

**Status:** ready-for-agent

- [ ] Clicking **Chat with this agent** makes a new chat with that agent included and the generated name, and the agent's reply streams in. Clicking again the same day makes a second chat with " (2)".
- [ ] Anyone in the organisation can find and read that chat, and its members can rename it. It shows the notice that the whole organisation can see it.
- [ ] A person not allowed to invoke the agent is refused with the reason.
- [ ] A browser test covers **Chat with this agent** and its notice.

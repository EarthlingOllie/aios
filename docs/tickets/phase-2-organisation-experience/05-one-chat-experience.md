# 05: One chat experience: read-only run page, "Chat with this agent"

**What to build:** Talking to a single agent happens in an open chat, like every other conversation.

- **Read-only run page.** The run page becomes a read-only trace: messages, tool calls, usage and version per turn. It links to the chat the run came from.
- **Removed.** The run page's own chat and the creation of empty runs are removed.
- **Chat with this agent.** A button on the agent's page makes a new open chat that includes that agent (and not the default agents), with the person in it, named "<Agent> – <person> – <date>". A clash adds " (2)", " (3)" and so on. Any chat member can rename it. It respects **invocable by**.
- **Old runs.** Runs made by the old run page chat stay readable as history.

[Spec](../../specs/phase-2-organisation-experience.md)

**Blocked by:** [02: Chats without owners, and archiving chats](02-chats-without-owners.md), [04: Streamed conversations and chat list, with limits and fallback](04-streaming.md)

**Status:** ready-for-agent

- [ ] The run page has no composer, and shows usage and version per turn.
- [ ] Runs from the old run page chat open read-only.
- [ ] Clicking **Chat with this agent** makes a new chat with that agent included and the generated name, and the agent's reply streams in. Clicking again the same day makes a second chat with " (2)".
- [ ] Anyone in the organisation can find and read that chat, and its members can rename it.
- [ ] A person not allowed to invoke the agent is refused with the reason.
- [ ] The run page chat route and the empty-run creation route are gone.
- [ ] The browser tests for run starters and the run page are updated.

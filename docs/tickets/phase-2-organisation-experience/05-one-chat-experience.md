# 05: One chat experience: read-only run page, "Chat with this agent"

**What to build:** Talking to a single agent happens in a direct message, like every other conversation.

- **Read-only run page.** The run page becomes a read-only trace: messages, tool calls, usage and version per turn. It links to the conversation the run came from, where the viewer may see it.
- **Removed.** The run page's own chat and the creation of empty runs are removed.
- **Chat with this agent.** A button on the agent's page opens the person's direct message with exactly that agent, reusing the most recently active one, or creates one. It respects **invocable by**.
- **Old runs.** Runs made by the old run page chat stay readable as history.

[Spec](../../specs/phase-2-organisation-experience.md)

**Blocked by:** [03: Direct messages, and copying one into a chat](03-direct-messages.md), [04: Streamed conversations and chat list, with limits and fallback](04-streaming.md)

**Status:** ready-for-agent

- [ ] The run page has no composer, and shows usage and version per turn.
- [ ] Runs from the old run page chat open read-only.
- [ ] Clicking **Chat with this agent** twice opens the same direct message, and the agent's reply streams in.
- [ ] A person not allowed to invoke the agent is refused with the reason.
- [ ] The run page chat route and the empty-run creation route are gone.
- [ ] The browser tests for run starters and the run page are updated.

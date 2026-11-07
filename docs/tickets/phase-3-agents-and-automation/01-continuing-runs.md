# 01: Continuing runs on the current version, with queued mentions

**What to build:** An agent becomes a persistent member of each conversation it's in. Mentioning it in a chat or direct message adds a turn to its one run there, rather than starting a new run. Each turn runs on the agent's current version, records which version it used, and is briefed only on what has happened since the agent's previous turn. Mentions made while a turn is under way, or while the run waits on an approval, queue and run in message order.

- Workflow node runs, and runs started by hand, by triggers or by `start_workflow`, are never conversation runs. A trigger delivering to a chat posts by handback without joining the agent's conversation there.
- The run page lists every turn with its version, status and usage.

[Spec](../../specs/phase-3-agents-and-automation.md)

**Blocked by:** [Phase 1 · 12: Run service: queue, mentioned turns, handoff, scheduler](../phase-1-foundation/12-run-service.md), [Phase 2 · 03: Direct messages, and copying one into a chat](../phase-2-organisation-experience/03-direct-messages.md), [Phase 2 · 05: One chat experience: read-only run page, "Chat with this agent"](../phase-2-organisation-experience/05-one-chat-experience.md)

**Status:** ready-for-agent

- [ ] Two mentions of an agent in one chat give one run with two turns. Mentions in two chats give two separate runs, which may run turns at the same time.
- [ ] A first message in a direct message with an agent creates its conversation run there.
- [ ] After an editor saves a new version, the agent's next turn in an existing conversation runs on it and records it. Earlier turns still show their own version on the run page.
- [ ] Workflow node runs keep their pinned version. A run waiting on an approval finishes that turn on the version it started with.
- [ ] A current version that can't run (a tool has left the library, say) fails the turn with the reason, and the run keeps its last version.
- [ ] The brief for a later turn covers only the conversation since the agent's previous turn, within the existing window limits.
- [ ] Mentions made during a turn, or while the run waits on an approval, queue in message order, and their cards say "Waiting for an approval" where that applies. A cancelled approval frees the queue.
- [ ] A mention of an archived agent is refused on its card.
- [ ] The full-stack test covers a second mention continuing the same run after an edit to the agent.

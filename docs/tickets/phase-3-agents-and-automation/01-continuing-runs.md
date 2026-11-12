# 01: Continuing runs on the current version, with queued mentions

**What to build:** An agent becomes a persistent member of each conversation it's in. Mentioning it in a chat adds a turn to its one run there, rather than starting a new run. Each turn runs on the agent's current version (phase 1's turn record already holds which one), and is briefed only on what has happened since the agent's previous turn. Mentions made while a turn is under way, or while the run waits on an approval, queue and run in message order.

- Workflow node runs, and runs started by hand, by triggers or by `start_workflow`, are never conversation runs. A trigger delivering to a chat posts by handback without joining the agent's conversation there.
- The run page lists every turn with its version, status and usage.

[Spec](../../specs/phase-3-agents-and-automation.md)

**Blocked by:** [Phase 1 · 12: Run service: queue, mentioned turns, handoff, scheduler](../phase-1-foundation/12-run-service.md), [Phase 2 · 01: Open chats, and the Jev check on posted messages](../phase-2-organisation-experience/01-open-chats.md), [Phase 2 · 05: One chat experience: "Chat with this agent"](../phase-2-organisation-experience/05-one-chat-experience.md), [Phase 1 · 11: Usage ledger in GBP, caps, mention guards](../phase-1-foundation/11-usage-caps-and-mention-guards.md), [Phase 2 · 08: Handback, and delivery to chats](../phase-2-organisation-experience/08-handback-and-delivery.md)

**Status:** done

- [x] Two mentions of an agent in one chat give one run with two turns. Mentions in two chats give two separate runs, which may run turns at the same time.
- [x] After an editor saves a new version, the agent's next turn in an existing conversation runs on it. Earlier turns still show their own version on the run page.
- [x] Workflow node runs keep their pinned version. A run waiting on an approval finishes that turn on the version it started with.
- [x] A current version that can't run (a tool has left the library, say) fails the turn with the reason, and the run keeps its last version.
- [x] The brief for a later turn covers only the conversation since the agent's previous turn, within the existing window limits.
- [x] Mentions made during a turn, or while the run waits on an approval, queue in message order, and their cards say "Waiting for an approval" where that applies. A cancelled approval frees the queue.
- [x] A mention of an archived agent is refused on its card.
- [x] The full-stack test covers a second mention continuing the same run after an edit to the agent.

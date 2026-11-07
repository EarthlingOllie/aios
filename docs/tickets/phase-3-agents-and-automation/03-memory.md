# 03: Memory: tools, visibility, Memory tab

**What to build:** Agents remember facts across runs, and people can see and correct what they remember. An agent stores and forgets memories with new tools, and recalls them through the unified search. Each memory keeps the visibility of where it was learned: one learned in a direct message is only ever recalled in that direct message. The agent's page gets a Memory tab listing its memories, filtered to what the viewer may see.

[Spec](../../specs/phase-3-agents-and-automation.md)

**Blocked by:** [Phase 1 · 06: Unified search across documents, chats and runs](../phase-1-foundation/06-unified-search.md), [Phase 1 · 08: Audit log and approval rules](../phase-1-foundation/08-audit-log-and-approval-rules.md), [Phase 2 · 03: Direct messages, and copying one into a chat](../phase-2-organisation-experience/03-direct-messages.md)

**Status:** ready-for-agent

- [ ] `remember` stores a memory with the run's visibility. Storing the same text again (ignoring case and whitespace) with the same visibility refreshes the existing memory instead of duplicating it.
- [ ] `remember` refuses over the length limit, or at the per-agent cap, with a clear message. `forget` deletes one of the agent's own memories that's visible in that run.
- [ ] Recall through search returns only the calling agent's memories. A visibility matrix test passes across direct message A, direct message B, a chat, and a run with no conversation.
- [ ] Memories live where no data tool role can read or describe them.
- [ ] The Memory tab lists memories with where each was learned. Memories from a direct message are hidden from people outside it.
- [ ] Editors and admins edit or delete any memory they can see. Anyone in a direct message, whatever their role, can delete memories learned there.
- [ ] Edits and deletions are recorded in the audit log, without the content of direct-message memories.
- [ ] Memories survive archiving and unarchiving, are never pruned by retention, and are left out of exports.
- [ ] An agent version without `remember` still recalls its existing memories.

# 03: Memory: tools, recall, Memory tab

**What to build:** Agents remember facts across runs, and people can see and correct what they remember. An agent stores and forgets memories with new tools, and recalls them through the unified search. Jev checks each memory before it's stored, and recalled memories are always wrapped as untrusted. A memory isn't tied to the conversation it was learned in: the agent recalls it wherever it works. The agent's page gets a Memory tab listing its memories.

[Spec](../../specs/phase-3-agents-and-automation.md)

**Blocked by:** [Phase 1 · 06: Unified search across documents, chats and runs](../phase-1-foundation/06-unified-search.md), [Phase 1 · 08: Audit log and approval rules](../phase-1-foundation/08-audit-log-and-approval-rules.md), [Phase 1 · 15: Jev: classify untrusted content](../phase-1-foundation/15-jev-classification.md), [Phase 2 · 10: Export and import agents and workflows](../phase-2-organisation-experience/10-export-and-import.md)

**Status:** ready-for-agent

- [ ] `remember` stores a memory. Storing the same text again (ignoring case and whitespace) refreshes the existing memory instead of duplicating it.
- [ ] `remember` classifies the text with Jev first. Flagged text, or text Jev can't check, is refused and the agent gets an error; nothing is stored. A stored memory has the verdict `derived`.
- [ ] Recalled memories reach the model wrapped as untrusted.
- [ ] `remember` refuses over the length limit, or at the per-agent cap, with a clear message. `forget` deletes one of the agent's own memories.
- [ ] Recall through search returns only the calling agent's memories. A memory learned in one chat, or in a run with no conversation, is recalled in another chat.
- [ ] The Memory tab lists memories with where each was learned.
- [ ] Editors and admins edit or delete any memory; other roles can't.
- [ ] Edits and deletions are recorded in the audit log.
- [ ] Memories survive archiving and unarchiving, are never pruned by retention, and are left out of exports.
- [ ] An agent version without `remember` still recalls its existing memories.

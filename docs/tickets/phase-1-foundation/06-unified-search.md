# 06: Unified search across documents, chats and runs

**What to build:** Members and agents search documents, chat messages, briefs and past runs' messages together. Everyone in the organisation can find everything in it. A new `search` tool and the Search page read one index in the store; the old document search and history search tools keep working as the same search fixed to their kinds. Embedding happens in the background under a budget per store, falling back to keyword search.

- Writers index in the same transaction as their rows: chunks at ingest, messages when posted, user and assistant text when a turn ends (not tool results), briefs at handoff. `make seed` indexes dev data through the same writers.
- The Search page labels each result's kind, filters by kind, and keeps the ranking explanation behind a "Show ranking" toggle.
- Once history search reads the new index, the run history export goes: its code, tasks, outbox, settings and panel, with a store migration that drops its copy from a store at any history revision.

[Spec](../../specs/phase-1-foundation.md)

**Blocked by:** [05: Triggers, chats, invocations and schema change requests move](05-move-triggers-chats-approvals.md)

**Status:** ready-for-agent

- [ ] One search finds a document, a chat message and a past run's message, each labelled with its kind and linked to its source.
- [ ] An agent finds any chat's messages and briefs, whichever chat or schedule started it, and the Search page shows a person everything in the organisation.
- [ ] Agent versions using the old document search or history search keys still run and get their old result shape.
- [ ] With embeddings unavailable, search returns keyword results, and unembedded items are embedded by a later sweep; unchanged text isn't re-embedded.
- [ ] Browser test covers the kind filter and the ranking toggle.
- [ ] The run history export panel, settings and endpoints are gone, and the export's tests are removed with the suite passing.
- [ ] Stores at any history revision migrate cleanly to the drop of the export's copy.

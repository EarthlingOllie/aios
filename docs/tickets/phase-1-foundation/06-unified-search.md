# 06: Unified search across documents, chats and runs

**What to build:** Members and agents search documents, chat messages, briefs and past runs' messages together, seeing only what they're allowed to see. A new `search` tool and the Search page read one index in the store; the old document search and history search tools keep working as the same search fixed to their kinds. Visibility follows the chat: a private chat's content is found only from inside it, a run started outside any chat sees only organisation-wide items, and the Search page shows people their own private chats and nothing else, with no hint of hidden results. Embedding happens in the background under a budget per store, falling back to keyword search.

- Writers index in the same transaction as their rows: chunks at ingest, messages when posted, user and assistant text when a turn ends (not tool results), briefs at handoff; existing content is backfilled.
- The Search page labels each result's kind, filters by kind, and keeps the ranking explanation behind a "Show ranking" toggle.

[Spec](../../specs/phase-1-foundation.md)

**Blocked by:** [05: Triggers, chats, invocations and schema change requests move](05-move-triggers-chats-approvals.md)

**Status:** ready-for-agent

- [ ] One search finds a document, a chat message and a past run's message, each labelled with its kind and linked to its source.
- [ ] A private chat's messages and briefs are found by an agent only when its invocation came from that chat; a run started outside any chat finds only organisation-wide items.
- [ ] The Search page shows a person only organisation-wide items and private chats they're in, with no hint of hidden results.
- [ ] Agent versions using the old document search or history search keys still run and get their old result shape.
- [ ] With embeddings unavailable, search returns keyword results, and unembedded items are embedded by a later sweep; unchanged text isn't re-embedded.
- [ ] Browser test covers the kind filter and the ranking toggle.

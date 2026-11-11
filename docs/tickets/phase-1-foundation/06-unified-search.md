# 06: Unified search across documents, chats and runs

**What to build:** Members and agents search documents, chat messages, briefs and past runs' messages together. Everyone in the organisation can find everything in it. A new `search` tool and the Search page read one index in the store; the old document search and history search tools keep working as the same search fixed to their kinds. Embedding happens in the background under a budget per store, falling back to keyword search.

- Writers index in the same transaction as their rows: chunks at ingest, messages when posted, user and assistant text when a turn ends (not tool results), briefs at handoff. `make seed` indexes dev data through the same writers.
- The Search page labels each result's kind, filters by kind, and keeps the ranking explanation behind a "Show ranking" toggle.
- Once history search reads the new index, the run history export goes: its code, tasks, outbox, settings and panel, with a store migration that drops its copy from a store at any history revision.

[Spec](../../specs/phase-1-foundation.md)

**Blocked by:** [05: Triggers, chats, invocations and schema change requests move](05-move-triggers-chats-approvals.md)

**Status:** done

- [x] One search finds a document, a chat message and a past run's message, each labelled with its kind and linked to its source.
  (Store revision 0015: `aios.search_items`, one item per document chunk, chat message, user's or
  assistant's turn message, and brief, each with a cascading foreign key to its source and its
  chat, agent, run and author for filters, a generated `tsv` and a nullable `embedding`. Writers
  index in their own transaction (`aios.search_index`): ingest, `post_message` and a run's posted
  result, `_end_turn` (the turn's new user and assistant text, not tool results), the handoff's
  brief; `make seed` posts its chat's first message through `post_message`. `aios.search` is one
  hybrid search over it; `GET /api/o/{org}/search` returns each item with its `kind` and its
  source's ids and names. `test_search.py`: `test_one_search_finds_...`,
  `test_a_turn_indexes_...`, `test_deleting_a_source_...`, `test_make_seed_indexes_...`.)
- [x] An agent finds any chat's messages and briefs, whichever chat or schedule started it, and the Search page shows a person everything in the organisation.
  (The new `search` tool (key `search`): query, kinds, since/until, chat, agent and limit; settings
  `max_results` (8, up to 50) and the kinds allowed; the run searching is left out. Items have no
  visibility, and the endpoint filters by nothing but kind. `test_an_agent_finds_any_chats_...`
  (a mentioned agent in another chat), `test_a_run_by_hand_or_by_schedule_...`,
  `test_the_search_page_shows_a_person_everything_...` (a private chat they're not in).)
- [x] Agent versions using the old document search or history search keys still run and get their old result shape.
  (`hybrid_search` and `history_search` stay registered, as the search fixed to
  `document_chunk` or `run_message`, with their result shapes and settings unchanged.
  `test_the_history_search_key_still_runs_...`, `test_the_document_search_key_still_runs_...`,
  `test_tools.py`.)
- [x] With embeddings unavailable, search returns keyword results, and unembedded items are embedded by a later sweep; unchanged text isn't re-embedded.
  (Items are written unembedded (ingest no longer embeds; `chunks.embedding` is nullable). The
  provisioning worker's beat queues `aios.embed_search_items` for every ready store every
  `SEARCH_EMBED_INTERVAL_SECONDS`; `embed_pending` embeds under `SEARCH_EMBED_BUDGET_SECONDS`,
  holding no store session while the API answers. `text_hash` keeps unchanged text's embedding
  on rewrite, and reuses an embedding of the same text in the store. A query that can't be
  embedded searches by keyword. `test_with_embeddings_unavailable_...`,
  `test_unchanged_text_isnt_embedded_again`, `test_an_item_written_again_...`,
  `test_the_sweep_embeds_under_a_budget_...`.)
- [x] Browser test covers the kind filter and the ranking toggle.
  (The Search page labels and links each result by kind, filters by kind (in the URL) and keeps
  the ranks and score behind "Show ranking". `e2e/search.spec.ts`.)
- [x] The run history export panel, settings and endpoints are gone, and the export's tests are removed with the suite passing.
  (Gone: `aios.datastores.history`, its tasks and beat entry, `api/settings.py` (the settings
  endpoints held only the export's fields), the export panel and `lib/settings.ts`, and
  `test_history_export.py`, `test_history_search.py`, `e2e/run-history-export.spec.ts`. Platform
  revision 0065 drops `history_exports` and the export's columns on `data_stores`.
  `test_the_run_history_exports_settings_and_endpoints_are_gone`, and the panel's absence in
  `e2e/search.spec.ts`.)
- [x] Stores at any history revision migrate cleanly to the drop of the export's copy.
  (History line revision h0005 drops the `history` schema; a store at h0001 to h0004 is brought
  there as it's migrated, and no store gets the line any more. `test_provisioning.py`:
  `test_a_store_at_any_history_revision_...` (each revision, with a copied run),
  `test_a_new_store_never_gets_the_history_line`, `test_the_history_drop_can_be_undone_...`.)

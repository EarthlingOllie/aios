# 11: Usage ledger in GBP, caps, mention guards

**What to build:** Every billable call writes one row to a usage ledger, priced in pounds sterling from a configured price table and attributed to its agent or workflow, run, turn, invocation, the person charged and the cause: model calls for turns and the handoff, embeddings for ingest, search queries and indexing, web searches, tool calls and sandbox time. A usage reporting view lets agents query the ledger without text. Agents and workflows gain the invocable-by and limits fields in the store. Admins see totals by agent, workflow, person and day. Turns and workflow runs are capped on model calls, tokens and cost, with limits that editors can lower and only admins can raise. Mentions are guarded in a fixed order before anything costs money, and participants can then mention.

- Checks at posting: at most five agents and workflows per message (422, nothing written); each mention's invocable-by (refused card, no model call); a per-person rate limit on allowed mentions (429 with a retry time, draft kept); a budget placeholder; then the handoff.
- A turn that hits a cap fails naming the limit and keeps what it got to.

[Spec](../../specs/phase-1-foundation.md)

**Blocked by:** [06: Unified search across documents, chats and runs](06-unified-search.md), [07: Roles and permissions on every route](07-roles-and-permissions.md), [10: An invocation for every run, and turns as records](10-invocations-and-turns.md)

**Status:** ready-for-agent

- [ ] A turn with N model calls writes exactly N model-call rows, counted with the scripted model; handoff calls, embedding batches, web search counts and sandbox seconds each write rows with the right purpose and person, and retries never double-count.
- [ ] The usage reporting view is readable through `query_data` and holds no text.
- [ ] Costs are in GBP; a model with no price records a blank cost; usage totals are admins-only.
- [ ] An agent capped at three model calls stops before a fourth, with a message naming the limit and its partial reply kept.
- [ ] An editor raising a limit above the organisation's answers 403; the limit in force follows the agent or workflow, then the organisation, then the deployment.
- [ ] A message mentioning six agents answers 422 and writes nothing; people mentioned don't count.
- [ ] A mention refused by invocable-by posts with a refused card, makes no model call and doesn't count towards the rate limit.
- [ ] Going over the mention rate limit answers 429 with a retry time, and the composer keeps the draft.
- [ ] Participants can mention; browser tests cover refused cards, the composer's 422 and 429 messages, and the invocable-by and limits controls.

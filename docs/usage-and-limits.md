# Usage, limits and mention guards

What agents and workflows use is recorded, priced and capped, and mentions are checked before
anything costs money.

## The usage ledger

Every billable call writes one row of `aios.usage`, in the organisation's store (`aios.usage`):

| Kind | Recorded by | Purpose |
| --- | --- | --- |
| `model_call` | a middleware on every agent (`UsageMiddleware`), after each model call of a turn, in the same transaction as the turn's counters; the handoff, with its brief | `turn`, `handoff` |
| `web_search` | the same middleware, from the response (the provider's count, else its web search blocks) | `turn` |
| `embedding` | `aios.embeddings`, once per batch: a search's query, `write_data`'s texts, and the background sweep (documents' chunks as `ingest`, everything else as `index_embedding`) | `search_query`, `index_embedding`, `ingest` |
| `sandbox_seconds` | a sandbox session ending: replaced by a new one, or stopped with its run | `turn` |

Tool calls get rows once a later phase prices them. Each row carries the model, tokens (cache
reads and writes too), a quantity, the estimated cost in pounds sterling, and what it's charged to:
the agent or workflow, the agent run and workflow run, the turn, the invocation, the person and
the cause. A turn's charge is set as a context variable while its graph runs (`usage.charging`),
so its tools' embeddings are charged to it; the Search page charges the person searching.

- **Prices** come from `PRICE_TABLE`, a JSON object by model (`provider:model`): per million
  tokens of `input`, `output`, `cache_read` and `cache_write`, per `web_search`, and `sandbox`'s
  `per_second`. A model with no price records a null cost; nothing fails.
- **Retries never count twice**: a model call's row is keyed by its response's id.
- It's never pruned (`aios.retention` leaves it), and has no foreign keys, so deleting a run
  leaves its usage.
- **Reading it.** Admins read totals by agent, workflow, person or day over a range of days
  (`GET /api/o/{org}/usage?group=&start=&end=`, the Organisation page's Usage tab). Agents read
  the rows through `reporting.usage` with `query_data`: identifiers, counts, costs and kinds, no
  text.

## Limits

A turn stops before a model call that would pass one of its caps, failing with a message naming
the limit ("Limit reached: at most 3 model calls per turn.") and keeping what it got to; a
mention's card posts the partial reply with it. A workflow agent node's turn checks its workflow
run's caps too. LangGraph's recursion limit follows the model-call cap.

| Cap | Setting (default) |
| --- | --- |
| Model calls per turn | `TURN_MAX_MODEL_CALLS` (200) |
| Tokens per turn | `TURN_MAX_TOKENS` (5,000,000) |
| Cost per turn (£) | `TURN_MAX_COST` (25) |
| Model calls per workflow run | `WORKFLOW_RUN_MAX_MODEL_CALLS` (2,000) |
| Cost per workflow run (£) | `WORKFLOW_RUN_MAX_COST` (100) |

The limit in force is the agent's or workflow's own, else the organisation's (an admin's override,
`PUT /api/o/{org}/organisation/limits`, on the Settings tab), else the deployment's. Editors set an
agent's turn caps and a workflow's run caps in their editors, lower only: setting one above the
organisation's is a 403 for anyone but an admin (`aios.limits`).

## Mention guards

Posting a message checks its mentions in this order (`aios.guards`):

1. **Count.** At most `MENTIONS_PER_MESSAGE` (5) distinct agents and workflows; past it, a 422
   and nothing is written. People don't count.
2. **Permission.** Each target's **invocable by** (everyone, editors and admins, or admins only,
   set in its editor) against the poster's role. A refused mention is still posted, its invocation
   `refused` with the reason on its card, and calls no model.
3. **Rate.** Allowed mentions per person per organisation, counted in Redis
   (`MENTION_RATE_PER_PERSON`, `60/minute`); past it, a 429 with `Retry-After` and when to try
   again, and nothing is written. Refused mentions don't count. The composer keeps the draft.
4. **Budget.** Nothing yet.
5. **The handoff**, on the run service.

Schedules and manual starts skip the count and rate checks, and are capped and charged like any
turn. Participants mention like anyone else.

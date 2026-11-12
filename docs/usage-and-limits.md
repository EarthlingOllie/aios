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
| `tool_call` | `post_message`, with each post (free: cost 0) | `turn` |

Other tool calls get rows once a later phase prices them. Each row carries the model, tokens (cache
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
- **Reading it.** Admins read totals (model calls, tokens, embedded texts, web searches, tool
  calls, sandbox seconds and cost) by agent, workflow, person or day over a range of days
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
4. **Budget.** The organisation at its monthly budget, or the poster at their allowance (see
   below): every mention still allowed is posted `refused`, the reason on its card, and calls no
   model. The run service checks again before the handoff, for a mention queued as the budget
   was reached.
5. **The handoff**, on the run service.

Schedules and manual starts skip the count and rate checks, are refused at the budget too, and
are capped and charged like any turn. Participants mention like anyone else.

## Budgets

An organisation can set a ceiling on what it spends (`aios.budgets`), in the ledger's estimated
pounds sterling per calendar month in UTC. None is set by default, and then nothing is refused.

- **Budget and allowances.** Admins set a monthly budget and default monthly allowances per role
  on the Settings tab (`PUT /api/o/{org}/organisation/budget`), and override a member's allowance
  on the Members tab (`PUT .../organisation/members/{user_id}/allowance`; blank follows their
  role's). Changes take effect at once and are audited (`settings.changed`,
  `member.allowance_changed`). Every member reads the budget and this month's spend
  (`GET .../organisation/budget`); each member's spend is admins' only, unless usage by person is
  shown to everyone (as on Insights), and anyone else sees only their own.
- **Totals.** `aios.spend_totals` keeps a running total per month for the organisation (person
  0) and per person, added to in the same transaction as each priced ledger row, so checks read
  two rows rather than summing the ledger. The routine worker reconciles them against the ledger
  nightly, half an hour after `RETENTION_PRUNE_HOUR`, logging any drift
  (`aios.budgets.reconcile_all`).
- **Attribution.** Spending is charged to the person at the root of the chain of causes
  (`Invocation.charged_to`, the ledger's person): whoever mentioned an agent or workflow, started
  a run by hand, or posted the message that fired a chat-message trigger, through any
  `start_workflow` calls and triggers that followed. A chain a schedule, or an event with no
  person behind it, began counts only against the organisation's budget.
- **Refusals.** At 100% of the budget, or of the root person's allowance, new work is refused
  before any model call: a mention on its card, a schedule's or event trigger's firing on the
  trigger (`refused`, with why, audited as `trigger.refused`), an agent's `start_workflow` call
  as the tool's result, and a run started by hand as a 409. Runs already going, and resumes of
  waiting runs, aren't new work and finish, even past the budget.
- **Notifications.** Every admin is warned once a month when spending passes
  `BUDGET_WARNING_THRESHOLD` (0.8) of the budget (`budget_warning`), and told once when it reaches
  it (`budget_reached`); lowering the budget below this month's spend tells them at once.

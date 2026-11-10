# 02: Compaction of long conversations

**What to build:** Long conversations keep working. Once the model's input for a turn passes a configurable threshold, the oldest whole turns are summarised in what the model sees, and the latest turns stay verbatim. Every turn stays visible to people and findable through search; only the model's working view is shortened. Summaries are derived text, always wrapped as untrusted when the model reads them.

[Spec](../../specs/phase-3-agents-and-automation.md)

**Blocked by:** [01: Continuing runs on the current version, with queued mentions](01-continuing-runs.md), [Phase 1 · 09: Retention for runs and chats](../phase-1-foundation/09-retention.md), [Phase 1 · 11: Usage ledger in GBP, caps, mention guards](../phase-1-foundation/11-usage-caps-and-mention-guards.md), [Phase 1 · 15: Jev: classify untrusted content](../phase-1-foundation/15-jev-classification.md)

**Status:** ready-for-agent

- [ ] Crossing the threshold writes a summary for the run, folding in any previous summary, and keeps the configured number of latest turns verbatim.
- [ ] A summary never separates a tool call from its result, so the model provider accepts the history.
- [ ] Each compaction is a usage ledger entry with cause "compaction", traced and tagged as such.
- [ ] The run's stored history is unchanged, and every turn is still searchable.
- [ ] The run page marks where earlier turns were summarised.
- [ ] The model sees each summary wrapped as untrusted, with the verdict `derived`.
- [ ] Retention prunes a conversation run turn by turn. A summary is removed only once every turn it covers has been pruned.
- [ ] The threshold, number of turns kept and summary size are settings with the spec's defaults.

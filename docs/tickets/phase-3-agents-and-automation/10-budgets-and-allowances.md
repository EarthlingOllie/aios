# 10: Budgets and allowances in GBP

**What to build:** Spending has a ceiling the organisation sets. Admins set a monthly budget in estimated pounds sterling (GBP) per calendar month in UTC. They can also set default allowances per role and override them per person. Allowances are charged to whoever started the chain of causes. Admins are warned at 80%. At 100%, new work is refused before any model call, while work already going finishes.

[Spec](../../specs/phase-3-agents-and-automation.md)

**Blocked by:** [Phase 1 · 11: Usage ledger in GBP, caps, mention guards](../phase-1-foundation/11-usage-caps-and-mention-guards.md)

**Status:** ready-for-agent

- [ ] Running totals are kept for the organisation and per person, updated with every usage ledger entry, and reconciled nightly against the ledger.
- [ ] At 100% of the budget, new invocations are refused before the handoff. The refusal shows on the card, on the trigger, and as a tool result, and costs no model call.
- [ ] Runs already going, and resumes of waiting runs, finish.
- [ ] Admins get a warning once per threshold per month, and a notification when the budget is reached.
- [ ] Allowances are charged to the root person, including through chat-message triggers and `start_workflow`. Schedules and events with no person count only against the organisation budget.
- [ ] The organisation Settings tab shows the budget and this month's spend. The Members tab shows each person's allowance and spend, and admins can override it.
- [ ] Budget and allowance changes take effect at once, and are audited.
- [ ] With no budget set, behaviour is unchanged.

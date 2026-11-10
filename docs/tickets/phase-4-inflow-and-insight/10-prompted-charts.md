# 10: Ask for a chart, and saved charts

**What to build:** Anyone can ask for a chart in words. The built-in insights agent returns a title, one SQL statement and a Vega-Lite spec; the SQL runs as the metrics-only insights role through the capped, read-only path, and the spec is checked against the allowlist before it's drawn in the app's themes. The asker can always see the SQL. Each question is an invocation charged to whoever asked and rate-limited per person.

Members can save a chart to the organisation's Insights page, where it re-runs its SQL on current data when viewed. Its creator or an admin renames or deletes it, and a chart whose query no longer runs says so and shows its SQL.

[Spec](../../specs/phase-4-inflow-and-insight.md)

**Blocked by:** [09: Insights page and every panel](09-insights.md), [Phase 3 · 10: Budgets and allowances in GBP](../phase-3-agents-and-automation/10-budgets-and-allowances.md)

**Status:** ready-for-agent

- [ ] The insights-role adversarial test passes: no access to conversation content, memory, synced text, base tables or another store.
- [ ] Every refused spec feature (external data, links, images, signal parameters, expression strings) is rejected; a failing spec is sent back to the agent once, then the error is shown with the SQL.
- [ ] Row, byte and time caps apply, and truncated results are drawn and marked as truncated.
- [ ] Asking past the per-person rate limit answers 429; budget or allowance refusals follow phase 3's contract; each question appears in the usage ledger against the asker.
- [ ] A saved chart re-runs its SQL (not the agent) on current data, under the same role and caps.
- [ ] Renaming or deleting a saved chart is refused (403) for anyone but its creator or an admin; saves and deletes are audited.
- [ ] A saved chart whose query no longer runs shows "this chart's query no longer works" with the SQL.

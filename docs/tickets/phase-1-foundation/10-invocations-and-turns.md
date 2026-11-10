# 10: An invocation for every run, and turns as records

**What to build:** Every run now records why it ran and who or what caused it: a mention, a schedule, or a person starting it by hand, with parent invocations, an ancestor chain and a depth for workflow agent nodes. Each turn becomes a record with its invocation, the agent version it ran on, its outcome, timings and counters; a run's status and counters derive from its turns (the run's own status and counter columns are dropped in the same change), the run page lists the turns, and reporting views expose runs and turns to agents without any text.

[Spec](../../specs/phase-1-foundation.md)

**Blocked by:** [05: Triggers, chats, invocations and schema change requests move](05-move-triggers-chats-approvals.md)

**Status:** ready-for-agent

- [ ] Every new run has exactly one invocation with the right cause: mention, schedule or manual.
- [ ] A workflow agent node's invocation is a child of its workflow run's invocation, with the chain and depth set.
- [ ] Each turn records the version it ran on, and run lists and the run page show the same totals as before.
- [ ] The run page lists a run's turns.
- [ ] The reporting views of runs and turns hold no message text.

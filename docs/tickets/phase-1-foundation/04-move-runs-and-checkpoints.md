# 04: Runs, checkpoints and workflow runs move

**What to build:** Agent runs, sandbox sessions, workflow runs and their steps move into the store, and each store gets its own checkpointer, connecting as `_app` through the pooler, with every run's history copied across. The run page, run cards and chat turns read and write the store, turn leases live on the store's run rows, and waiting and resumed runs survive the move.

[Spec](../../specs/phase-1-foundation.md)

**Blocked by:** [01: Connection pooler and turn lease](01-pooler-and-turn-lease.md), [03: Agents, skills and workflows move to the store](03-move-agents-skills-workflows.md)

**Status:** ready-for-agent

- [ ] After backfill, every run's latest checkpoint in the store matches the platform's, and message counts agree.
- [ ] A new turn on a migrated run continues its existing history.
- [ ] Deleting a run removes its history from the store.
- [ ] Workflow runs waiting on an approval before the move resume correctly after it.
- [ ] The run page's graph, steps and conversations match what they showed before the move.
- [ ] Trace sessions carry the organisation as well as the run.
- [ ] The verifier is clean, and reads can be switched back.

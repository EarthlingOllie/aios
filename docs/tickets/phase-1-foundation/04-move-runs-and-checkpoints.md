# 04: Runs, checkpoints and workflow runs move

**What to build:** Agent runs, sandbox sessions, workflow runs and their steps move into the store in one change that also deletes their old platform tables, and each store gets its own checkpointer, connecting through the pooler. The run page, run cards and chat turns read and write the store, and turn leases live on the store's run rows. Dev data is rebuilt with `make seed`.

[Spec](../../specs/phase-1-foundation.md)

**Blocked by:** [01: Connection pooler and turn lease](01-pooler-and-turn-lease.md), [03: Agents, skills and workflows move to the store](03-move-agents-skills-workflows.md)

**Status:** ready-for-agent

- [ ] A new turn on an existing run continues its history from the store's checkpointer.
- [ ] Deleting a run removes its history from the store.
- [ ] A workflow run waiting on an approval resumes correctly once it's decided.
- [ ] The run page's graph, steps and conversations show as before, read from the store.
- [ ] Trace sessions carry the organisation as well as the run.
- [ ] The platform database no longer holds run, sandbox session, workflow run, step or checkpoint tables.

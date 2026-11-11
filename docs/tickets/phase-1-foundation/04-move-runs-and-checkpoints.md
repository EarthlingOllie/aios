# 04: Runs, checkpoints and workflow runs move

**What to build:** Agent runs, sandbox sessions, workflow runs and their steps move into the store in one change that also deletes their old platform tables, and each store gets its own checkpointer, connecting through the pooler. The run page, run cards and chat turns read and write the store, and turn leases live on the store's run rows. Dev data is rebuilt with `make seed`.

[Spec](../../specs/phase-1-foundation.md)

**Blocked by:** [01: Connection pooler and turn lease](01-pooler-and-turn-lease.md), [03: Agents, skills and workflows move to the store](03-move-agents-skills-workflows.md)

**Status:** done

- [x] A new turn on an existing run continues its history from the store's checkpointer.
  (Store revision 0010 makes `agent_runs`, `sandbox_sessions`, `workflow_runs` and
  `workflow_run_steps` in every store, and the store's own checkpointer's tables in its
  `langgraph` schema, set up as the store's owner before its first turn and again on every
  migration (`checkpoints.set_up_store`). The checkpointer connects as the store's read-write
  role through the pooler (`datastores.store_conninfo`), finding its tables by the role's search
  path in the store; turn leases are on the store's run rows, fenced on them.
  `test_runs_in_store.py::test_a_new_turn_continues_the_runs_history_from_the_stores_checkpointer`.)
- [x] Deleting a run removes its history from the store.
  (In the store transaction that deletes the run, after locking its row, which a checkpoint
  write being made holds: `test_runs_in_store.py::test_deleting_a_run_removes_its_history_from_the_store`,
  and `test_agents.py::test_deleting_a_run_mid_turn_deletes_all_its_checkpoints`.)
- [x] A workflow run waiting on an approval resumes correctly once it's decided.
  (Worker tasks carry the store with the run: `run_workflow_task(data_store_id, run_id)`,
  `resume_agent_run_task(data_store_id, run_id)`; `test_workflow_pause.py` passes, and the
  full-stack core loop.)
- [x] The run page's graph, steps and conversations show as before, read from the store.
  (The API's shape is unchanged; the browser tests, against their fake API, and the full-stack
  core loop pass as they were.)
- [x] Trace sessions carry the organisation as well as the run.
  (`agent-run-<organisation>-<id>`, `tracing.run_session`; threads keep their names.
  `test_organisation_runs.py::test_traces_carry_the_organisation_and_who_started_the_run`.)
- [x] The platform database no longer holds run, sandbox session, workflow run, step or checkpoint tables.
  (Platform revision 0062 drops them, the `langgraph` schema and the role's search path for it;
  schedules, chat invocations and schema change requests keep run ids with no foreign key, and
  are cleared when the run is deleted (`invocations.forget_runs`). Its downgrade brings them
  back empty, the schema identical to 0061's. `make seed` rebuilds dev data with them in the
  store. `test_runs_in_store.py::test_the_platform_holds_no_runs_and_no_checkpoints_and_each_store_does`.)

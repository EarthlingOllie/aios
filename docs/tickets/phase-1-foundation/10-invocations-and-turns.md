# 10: An invocation for every run, and turns as records

**What to build:** Every run now records why it ran and who or what caused it: a mention, a schedule, or a person starting it by hand, with parent invocations, an ancestor chain and a depth for workflow agent nodes. Each turn becomes a record with its invocation, the agent version it ran on, its outcome, timings and counters; a run's status and counters derive from its turns (the run's own status and counter columns are dropped in the same change), the run page lists the turns, and reporting views expose runs and turns to agents without any text.

[Spec](../../specs/phase-1-foundation.md)

**Blocked by:** [05: Triggers, chats, invocations and schema change requests move](05-move-triggers-chats-approvals.md)

**Status:** done

- [x] Every new run has exactly one invocation with the right cause: mention, schedule or manual.
  (Store revision 0013 gives invocations `trigger_id`, `parent_id`, `ancestor_ids` and `depth`.
  `new_agent_run` and `new_workflow_run` make the run's invocation with it
  (`aios.invocations.for_run`): `manual` by the member, `schedule` naming its trigger; a
  mention's run keeps its mention's. One no chat follows finishes, posting nothing, when an
  agent run's turn ends (`finish_quietly`) or its workflow run ends. The run page's and workflow
  run page's API returns it (`invocation`). `test_invocations_and_turns.py`: by hand, agent and
  workflow schedules, a mention.)
- [x] A workflow agent node's invocation is a child of its workflow run's invocation, with the chain and depth set.
  (`_start_step` makes it with the node's agent run, taking the parent's cause, ancestors
  `[*parent.ancestor_ids, parent.id]` and depth + 1; a check keeps `depth` the chain's length.
  Nodes of a subworkflow are children of the same workflow run's, as part of the same run. A
  mentioned workflow's nodes take `mention` as their cause without naming the chat, which
  `ck_invocations_mention` now allows below the root.)
- [x] Each turn records the version it ran on, and run lists and the run page show the same totals as before.
  (`aios.turns` table: run, invocation, agent version, status and error, times, model calls,
  tool calls and tokens. Every turn begins (`_start_turn`, or lazily for a resume) and ends
  (`_end_turn`) by its id; chats, schedules, mentions, resumes and workflow agent nodes alike.
  The run's `status`, `started_at`, `finished_at`, `tool_call_count`, `input_tokens` and
  `output_tokens` columns are dropped; `aios.turns.run_columns` derives them (latest turn's
  status and times, summed counters) for the run list, the run page and chat cards, in the same
  shape. One difference: a run whose only turn failed before any model call shows 0 rather than
  null counters, which the UI shows the same. Stalled turns are failed by turn now
  (`firing.fail_stalled_in`, `fail_stopped_turns`). `test_agent_run_counters.py`,
  `test_invocations_and_turns.py`.)
- [x] The run page lists a run's turns.
  (`turns` on the run's API; a Turns tab on the run page with each turn's number, status,
  version, start, duration, model calls, tool calls, tokens and error. `e2e/run-turns.spec.ts`,
  `turnCounters` in `agent-runs.test.ts`.)
- [x] The reporting views of runs and turns hold no message text.
  (`reporting.runs` (agent and workflow runs) and `reporting.turns`, in a `reporting` schema the
  read-only role reads through default privileges and `describe_data` lists; ids, cause,
  statuses, times and counts only: no titles, inputs, outputs, errors or messages. Writes
  through them are revoked each migration. `test_invocations_and_turns.py` reads them with
  `query_data` after a turn whose text must not appear, and checks their only text columns are
  `kind`, `cause` and `status`.)

Merged after P1-08 was renumbered: this is store revision 0013, and P1-08's audit log and
approval rules are 0014, which adds `reporting.audit_log` to the `reporting` schema made here
(`IF NOT EXISTS`) and leaves dropping the schema to this revision's downgrade.

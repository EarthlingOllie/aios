# 13: Workflows node by node; approvals resume there; workflow worker removed

**What to build:** Workflows advance one node at a time on the run service instead of holding a worker for the whole run. Each advance rebuilds the run's state from its recorded steps and starts the nodes now ready; agent nodes run as child invocations; parallel branches run at the same time; subworkflows expand within their parent's advances; and the time limit counts active time only. Deciding an approval resumes the waiting node and advances its run without any worker holding it, and the old workflow worker, its tasks and its queue are removed.

[Spec](../../specs/phase-1-foundation.md)

**Blocked by:** [12: Run service: queue, mentioned turns, handoff, scheduler](12-run-service.md)

**Status:** done

- [x] A long single-node workflow holds no process between model calls.
  (Runs go node by node on the run service, `aios.workflows.advance`: a workflow advance item,
  under the run's row lock, rebuilds the run's state from its recorded steps and starts the
  nodes now ready; each agent node is a workflow node item, a child invocation of the run's.
  Test: while a long node waits on the model, only its node item is in flight, and the same
  process runs another workflow from start to end meanwhile.)
- [x] Two parallel branches run concurrently, and their advances are serialised per run.
  (One advance per run is queued at a time, deduplicated by run, and an advance enqueued while
  one runs makes it run once more when it's done, `work_items.again`; advances take the run's
  row lock. Tests: both branches' node items are in flight at once, never two advances; the
  fan-out/fan-in and run-again queue tests.)
- [x] A subworkflow's steps record the correct paths and outputs.
  (Workflow nodes are expanded within the parent's advances: the subworkflow's step starts with
  its first nodes' and finishes, waits or fails with its graph. The subgraph, failure, nested
  waiting and resume tests pass unchanged.)
- [x] A run's time limit excludes time spent waiting on approvals.
  (Checked as active time, `now - started_at - waited_seconds`, at each advance and as each
  node's timeout. Tests: a long wait isn't failed; a run resumes with only what was left.)
- [x] A run waiting on an approval goes from waiting to running once it's decided; an expired approval resumes it as a rejection.
  (A decision or expiry enqueues an advance of the run, `aios.run_service.work.enqueue_resume`,
  which resumes the waiting node on its agent run's thread; a standalone agent run's resume is a
  `resume_turn` item. Tests: the run reads running, holding nothing until then; approve,
  reject, expiry, nested and decided-meanwhile cases; the full-stack core loop passed against a
  local stack with the run service and no workflow worker.)
- [x] The workflow worker service, its queue and its tasks no longer exist in Compose or the AWS deployment.
  (`aios.run_workflow`, `aios.resume_agent_run`, the `workflows` queue, per-run heartbeats and
  the run's `_Heartbeat` are gone; Compose `workflow-worker`/`fullstack-workflow-worker` and
  Terraform's task definition, service, log group and `var.workflow_worker` removed.
  `docker compose config` checked; `terraform validate`/`plan` not run here (no terraform
  binary, and nothing applied). The no-progress limit is now the run service's watchdog.)
- [x] The workflow run tests are ported to the run service's drain-once entry point and pass.
  (`test_workflow_runs`, `test_workflow_pause`, and the chat, schedule, archive and store tests
  that ran workflows, through `conftest.run_workflow`/`drain_run`. Worker-death tests became
  run service ones: a node retried after its process died carries on its turn, a turn cut off
  part way carries on from its checkpoint, and one whose attempts run out fails with "The
  service stopped mid-run.")

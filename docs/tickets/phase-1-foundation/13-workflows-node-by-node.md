# 13: Workflows node by node; approvals resume there; workflow worker removed

**What to build:** Workflows advance one node at a time on the run service instead of holding a worker for the whole run. Each advance rebuilds the run's state from its recorded steps and starts the nodes now ready; agent nodes run as child invocations; parallel branches run at the same time; subworkflows expand within their parent's advances; and the time limit counts active time only. Deciding an approval resumes the waiting node and advances its run without any worker holding it, and the old workflow worker, its tasks and its queue are removed.

[Spec](../../specs/phase-1-foundation.md)

**Blocked by:** [12: Run service: queue, mentioned turns, handoff, scheduler](12-run-service.md)

**Status:** ready-for-agent

- [ ] A long single-node workflow holds no process between model calls.
- [ ] Two parallel branches run concurrently, and their advances are serialised per run.
- [ ] A subworkflow's steps record the correct paths and outputs.
- [ ] A run's time limit excludes time spent waiting on approvals.
- [ ] A run waiting on an approval goes from waiting to running once it's decided; an expired approval resumes it as a rejection.
- [ ] The workflow worker service, its queue and its tasks no longer exist in Compose or the AWS deployment.
- [ ] The workflow run tests are ported to the run service's drain-once entry point and pass.

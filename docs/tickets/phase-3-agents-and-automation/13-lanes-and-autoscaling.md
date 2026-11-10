# 13: Lanes and autoscaling

**What to build:** Phase 4's syncs get a fair lane of their own, urgent maintenance never waits behind routine sweeps, and capacity follows demand. (Interactive work's reserved capacity already ships in Phase 1 · 12.)

- **Lanes.** The run service gains the `sources` lane for phase 4's syncs, beside interactive and background. Ingest, urgent maintenance and routine maintenance get their own Celery queues and workers.
- **Autoscaling.** Queue metrics drive scaling up and down to configurable floors and ceilings.

[Spec](../../specs/phase-3-agents-and-automation.md)

**Blocked by:** [Phase 1 · 12: Run service: queue, mentioned turns, handoff, scheduler](../phase-1-foundation/12-run-service.md)

**Status:** ready-for-agent

- [ ] Work queued on the `sources` lane is claimed fairly between organisations, and never takes interactive work's reserved capacity.
- [ ] Fairness between organisations holds within each lane.
- [ ] Approved changes are applied, and restores run, on the urgent lane, never behind migrations or sweeps. The beat only enqueues.
- [ ] The provisioning queue is retired once drained. Only the maintenance workers hold the provisioner's credentials.
- [ ] Depth and the age of the oldest ready item are published per lane and queue at the configured interval.
- [ ] Run service processes scale on interactive latency and background and sources depth, and ingest workers on ingest depth. Every service has a configurable minimum and maximum.
- [ ] Idle services return to their floor after the scale-in cooldown, relying on graceful shutdown.

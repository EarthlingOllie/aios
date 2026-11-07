# 13: Lanes and autoscaling

**What to build:** Someone waiting in a chat always comes first, urgent maintenance never waits behind routine sweeps, and capacity follows demand.

- **Lanes.** Interactive work has reserved slots on every executor. Ingest, urgent maintenance and routine maintenance get their own queues and workers.
- **Autoscaling.** Queue metrics drive scaling up and down to configurable floors and ceilings.

[Spec](../../specs/phase-3-agents-and-automation.md)

**Blocked by:** [Phase 1 · 12: Run service: queue, mentioned turns, handoff, scheduler](../phase-1-foundation/12-run-service.md)

**Status:** ready-for-agent

- [ ] Under a background flood, a mention is claimed within the latency target from a reserved slot (tested with a fake executor). Background work never takes reserved slots.
- [ ] Fairness between organisations holds within each lane.
- [ ] Approvals, restores, table access changes and agent login creation run on the urgent lane, never behind migrations or sweeps. The beat only enqueues.
- [ ] The provisioning queue is retired once drained. Only the maintenance workers hold the provisioner's credentials.
- [ ] Depth and the age of the oldest ready item are published per lane and queue at the configured interval.
- [ ] Executors scale on interactive latency and background depth, and ingest workers on ingest depth. Every service has a configurable minimum and maximum.
- [ ] Idle services return to their floor after the scale-in cooldown, relying on graceful shutdown.

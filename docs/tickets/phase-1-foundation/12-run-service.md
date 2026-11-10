# 12: Run service: queue, mentioned turns, handoff, scheduler

**What to build:** A new run service takes agent work out of the web servers. A work queue in the platform database serves people waiting in chat before background work, and the organisation with the least in flight first, under a soft per-organisation cap that admins can override. Mentioned agents' turns, the handoff before every mention, and the scheduler all run on it. Each run service process runs many turns at once, with leases, retries and a limit on attempts, and a "drain the queue once" entry point lets tests run it deterministically.

[Spec](../../specs/phase-1-foundation.md)

**Blocked by:** [01: Connection pooler and turn lease](01-pooler-and-turn-lease.md), [10: An invocation for every run, and turns as records](10-invocations-and-turns.md)

**Status:** ready-for-agent

- [ ] Starvation test: while one organisation floods the queue, another organisation's mentioned turn starts within the target time.
- [ ] An item whose lease expires is retried elsewhere, and after three attempts its invocation fails with "The service stopped mid-run."
- [ ] An organisation at its in-flight cap waits while other organisations' work is claimed; each process keeps reserved capacity for people waiting in chat.
- [ ] A lost enqueue is repaired by the requeue sweep.
- [ ] An admin raises their organisation's in-flight cap, and more of its work is then claimed at once; editors and participants can't change it.
- [ ] Agent and workflow mentions are briefed on the run service, and a failed handoff fails its invocation as today.
- [ ] Each run of a schedule fires exactly once across run service processes, the scheduler-enabled setting applies to them, and missed runs aren't caught up.
- [ ] Compose and the AWS deployment run the run service.

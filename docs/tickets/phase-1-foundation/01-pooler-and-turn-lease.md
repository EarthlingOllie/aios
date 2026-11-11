# 01: Connection pooler and turn lease

**What to build:** Stores are reached through a connection pooler (PgBouncer, in transaction mode) in Compose, CI and the AWS deployment, and a run's turns no longer depend on database session features to stay in order. Two turns on the same run are still answered one after the other, now through a turn lease on the run that is refreshed by the heartbeat, waited on through Redis, and fenced so a process that lost it can't write the run's history. Stopping a turn signals its holder instead of killing a pooled connection.

- Store connections and the checkpointer use no prepared statements, startup parameters or session-level settings.
- The lease replaces the advisory lock in the same change, and backend termination is removed.
- Per-process pool sizes shrink and the per-role connection limit becomes a backstop, as the spec sets out.

[Spec](../../specs/phase-1-foundation.md)

**Blocked by:** [Phase 0 · 02: Models split into a package by domain](../phase-0-refactor-and-guard-rails/02-models-package.md), [Phase 0 · 05: Backend suite sharded, in random order, with one checks job](../phase-0-refactor-and-guard-rails/05-sharded-backend-suite.md), [Phase 0 · 07: Archive agents](../phase-0-refactor-and-guard-rails/07-archive-agents.md)

**Status:** done

- [ ] The backend suite and the full-stack test pass with stores reached through the pooler, run as a CI service container.
  (CI-only to confirm. PgBouncer is a service container of each backend shard and a Compose
  service the full-stack stack's backend reaches stores through. Locally, the backend suite
  passed through PgBouncer 1.22 in transaction mode, apart from the known environment-only
  failures; the full-stack test needs Docker, which wasn't available. The AWS `pooler` service
  is written (`infra/terraform/pooler.tf`) but not applied: see docs/deploy.md on its auth user.)
- [x] Two turns posted to one run at once serialise, and the second starts from the history the first left.
- [x] A holder whose lease has lapsed can't write a checkpoint: its write fails and nothing is stored.
- [x] A run that stays busy past the configured wait answers 409 "This run is busy".
- [x] Stopping a turn ends it within a second; with Redis unavailable, the holder's next write fails on fencing.
- [x] Archiving an agent mid-turn lets the turn finish, as phase 0 decided.
- [x] No pooled connection is ever terminated to stop a turn, and frequently run queries prepare no statements.
- [x] The advisory turn lock is gone; the checkpointer's setup lock is held for one transaction only.
- [x] The deploy docs describe the pooler and its sizing.

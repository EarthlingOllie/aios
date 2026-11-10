# 01: Connection pooler and turn lease

**What to build:** Stores are reached through a connection pooler (PgBouncer, in transaction mode) in Compose, CI and the AWS deployment, and a run's turns no longer depend on database session features to stay in order. Two turns on the same run are still answered one after the other, now through a turn lease on the run that is refreshed by the heartbeat, waited on through Redis, and fenced so a process that lost it can't write the run's history. Stopping a turn signals its holder instead of killing a pooled connection.

- Store connections and the checkpointer use no prepared statements, startup parameters or session-level settings.
- The lease replaces the advisory lock in the same change, and backend termination is removed.
- Per-process pool sizes shrink and the per-role connection limit becomes a backstop, as the spec sets out.

[Spec](../../specs/phase-1-foundation.md)

**Blocked by:** [Phase 0 · 02: Models split into a package by domain](../phase-0-refactor-and-guard-rails/02-models-package.md), [Phase 0 · 05: Backend suite sharded, in random order, with one checks job](../phase-0-refactor-and-guard-rails/05-sharded-backend-suite.md), [Phase 0 · 07: Archive agents](../phase-0-refactor-and-guard-rails/07-archive-agents.md)

**Status:** ready-for-agent

- [ ] The backend suite and the full-stack test pass with stores reached through the pooler, run as a CI service container.
- [ ] Two turns posted to one run at once serialise, and the second starts from the history the first left.
- [ ] A holder whose lease has lapsed can't write a checkpoint: its write fails and nothing is stored.
- [ ] A run that stays busy past the configured wait answers 409 "This run is busy".
- [ ] Stopping a turn ends it within a second; with Redis unavailable, the holder's next write fails on fencing.
- [ ] Archiving an agent mid-turn lets the turn finish, as phase 0 decided.
- [ ] No pooled connection is ever terminated to stop a turn, and frequently run queries prepare no statements.
- [ ] The advisory turn lock is gone; the checkpointer's setup lock is held for one transaction only.
- [ ] The deploy docs describe the pooler and its sizing.

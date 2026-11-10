# 05: Per-agent database logins, with the escape test

**What to build:** The database itself enforces table ownership. Each agent that may write gets its own database login, granted write on exactly the data tables it owns or is shared, and read on the rest of the store, conversation tables included. `write_data` runs as that login. Model-written SQL keeps running on the store's read-only role, which can't write anything. An adversarial escape test becomes a required check.

- A group role carries the unowned tables. Agent logins inherit from it, but can't switch to it.
- Grants are re-derived from ownership and shares on every change (apply, share, unshare, transfer, archive, drop, restore, purge) by a task on the urgent maintenance lane from ticket 13. Access changes show as pending until applied.

[Spec](../../specs/phase-3-agents-and-automation.md)

**Blocked by:** [04: Table ownership, sharing and transfer](04-table-ownership.md), [Phase 1 · 01: Connection pooler and turn lease](../phase-1-foundation/01-pooler-and-turn-lease.md), [13: Lanes and autoscaling](13-lanes-and-autoscaling.md)

**Status:** ready-for-agent

- [ ] The store migration command creates and verifies the group role and every writing agent's login, with encrypted credentials sent to Postgres only as verifiers.
- [ ] An agent login holds write grants only on the data tables it owns or is shared, the unowned tables, and an explicit list of platform tables. It can read conversation content and memory but never write them. Phase 1's deny list (secret and credential columns, people's emails, audit log details) stays ungranted.
- [ ] The database refuses writes to tables the agent doesn't own or share, even with the application's own check bypassed.
- [ ] The escape test refuses or neutralises every case below, attempted both as an agent login and through `query_data`. It's a required check in the backend shards.
  The cases: resetting or switching role, setting the role through the configuration function, setting session authorisation, making the read-only transaction read-write, altering one's own role, security-definer functions, server file access, SQL injection through write-tool names, and connecting to another store or the platform database.
  Writing chat messages, briefs, run messages, checkpoints, or any agent's memory is also refused.
- [ ] Agent login creation and table access changes run on the urgent lane, never behind migrations or sweeps.
- [ ] A write whose role is still being set up answers "being set up, try again" without failing the turn.
- [ ] Archiving an agent closes its connections and revokes its grants. Unarchiving restores them with the same login.
- [ ] Agent logins connect through the pooler, with their own connection limit, and only for writes.

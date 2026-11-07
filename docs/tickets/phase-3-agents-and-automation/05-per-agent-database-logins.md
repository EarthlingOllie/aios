# 05: Per-agent database logins, with the escape test

**What to build:** The database itself enforces table ownership. Each agent that may write gets its own database login, granted exactly the data tables it owns or is shared, and nothing else. `write_data` runs as that login behind a per-store switch. Model-written SQL keeps running on the store's read-only role, which can't write anything. An adversarial escape test becomes a required check.

- A group role carries the unowned tables. Agent logins inherit from it, but can't switch to it.
- Grants are re-derived from ownership and shares on every change (apply, share, unshare, transfer, archive, drop, restore, purge) by an urgent maintenance task. Access changes show as pending until applied.

[Spec](../../specs/phase-3-agents-and-automation.md)

**Blocked by:** [04: Table ownership, sharing and transfer](04-table-ownership.md), [Phase 1 · 01: Connection pooler and turn lease](../phase-1-foundation/01-pooler-and-turn-lease.md)

**Status:** ready-for-agent

- [ ] The store migration command creates and verifies the group role and every writing agent's login, with encrypted credentials sent to Postgres only as verifiers.
- [ ] An agent login holds grants only on the data tables it owns or is shared, the unowned tables, and an explicit list of platform tables. It never holds conversation content or memory, its own included.
- [ ] With the per-store switch on, the database refuses writes to tables the agent doesn't own or share, even with the application's own check bypassed.
- [ ] The escape test refuses or neutralises every case below, attempted both as an agent login and through `query_data`. It's a required check in the backend shards.
  The cases: resetting or switching role, setting the role through the configuration function, setting session authorisation, making the read-only transaction read-write, altering one's own role, security-definer functions, server file access, SQL injection through write-tool names, and connecting to another store or the platform database.
  Reading or describing direct messages, chat messages, briefs, run messages, checkpoints, or any agent's memory is also refused. Search returns only what the run's visibility allows.
- [ ] A write whose role is still being set up answers "being set up, try again" without failing the turn.
- [ ] Archiving an agent closes its connections and revokes its grants. Unarchiving restores them with the same login.
- [ ] Agent logins connect through the pooler, with their own connection limit, and only for writes.

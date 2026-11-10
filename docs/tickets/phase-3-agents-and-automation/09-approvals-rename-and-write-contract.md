# 09: Remove old forms: approvals rename; read-write role loses data writes

**What to build:** The old forms are removed. The store's read-write role loses write access to data tables, so only agent logins write them. The schema change table becomes approvals in one change that also drops the old step reference and the old expiry setting. Development data is rebuilt with `make seed`.

[Spec](../../specs/phase-3-agents-and-automation.md)

**Blocked by:** [05: Per-agent database logins, with the escape test](05-per-agent-database-logins.md), [07: Approvals as one concept, and the approval node](07-approvals-and-approval-node.md)

**Status:** ready-for-agent

- [ ] The read-write role can't insert, update or delete data tables, and has no default privilege to.
- [ ] Platform writes (documents, chunks) are unaffected.
- [ ] The underlying table is renamed to approvals, with no view under the old name. The old step reference and the old expiry setting are gone.
- [ ] `make seed` rebuilds development data on the new schema, and the full suite passes.

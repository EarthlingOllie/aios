# 09: Remove old forms: approvals rename; read-write role loses data writes

**What to build:** The old forms are removed once nothing needs them. The store's read-write role loses write access to data tables, so only agent logins write them. The schema change table becomes approvals, with a compatibility view under the old name for one release. A following release drops the view, the old routes, the old step reference and the old expiry setting alias.

[Spec](../../specs/phase-3-agents-and-automation.md)

**Blocked by:** [05: Per-agent database logins, with the escape test](05-per-agent-database-logins.md), [07: Approvals as one concept, and the approval node](07-approvals-and-approval-node.md)

**Status:** ready-for-agent

- [ ] Once the per-store switch is on in every store, the read-write role can't insert, update or delete data tables, and has no default privilege to.
- [ ] Platform writes (documents, chunks) are unaffected.
- [ ] Rolling back is a store migration that restores the read-write role's grant.
- [ ] The underlying table is renamed to approvals. The previous release keeps working through the view under the old name.
- [ ] The next release removes the view, the old schema change routes, the old step reference and the old expiry setting alias.
- [ ] The full suite passes after each release.

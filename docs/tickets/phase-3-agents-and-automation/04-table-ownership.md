# 04: Table ownership, sharing and transfer

**What to build:** Every data table can have an owning agent, and only the owner and the agents it's shared with may write it. Applying a schema change that creates a table makes the requesting agent its owner. Tables without an owner stay writable by any agent until an editor assigns one; development data is rebuilt with `make seed`. Editors and admins see and manage access on the Data page: share, unshare and transfer. Archiving an owner makes its tables read-only until they're transferred. In this ticket the write tool enforces this; the database enforces it in the next.

[Spec](../../specs/phase-3-agents-and-automation.md)

**Blocked by:** [Phase 0 · 08: Archive workflows and skills; remove every hard delete](../phase-0-refactor-and-guard-rails/08-archive-workflows-and-skills.md), [Phase 1 · 05: Triggers, chats, invocations and schema change requests move](../phase-1-foundation/05-move-triggers-chats-approvals.md), [Phase 1 · 07: Roles and permissions on every route](../phase-1-foundation/07-roles-and-permissions.md), [Phase 1 · 08: Audit log and approval rules](../phase-1-foundation/08-audit-log-and-approval-rules.md)

**Status:** ready-for-agent

- [ ] Applying an approved create-table schema change records the requesting agent as the table's owner.
- [ ] Unowned tables stay writable by any agent with `write_data`, and the Data page marks them "No owner". An editor can assign an owner at any time.
- [ ] `write_data` refuses agents that neither own nor share an owned table, naming the owner.
- [ ] Editors and admins share, unshare and transfer tables on the Data page. Each change respects the approval rules and is recorded in the audit log.
- [ ] Sharing with an archived agent, sharing twice, or sharing with the owner is refused (409).
- [ ] Archiving an owner makes its tables read-only for every agent, including those it's shared with, until they're transferred. The archive dialog lists those tables.
- [ ] Ownership and shares follow a table through drop and restore, and are deleted when it's purged.
- [ ] Every agent can still read every table through `query_data`.

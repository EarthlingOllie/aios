# 08: Audit log and approval rules

**What to build:** Admins get an audit log in the store that the application itself can't alter. It records configuration changes, archive and unarchive, approval decisions, role, membership, invitation and rename changes, settings changes and each `write_data` call. Admins read it on a new Audit log tab, filtered by actor, action and date; agents read a reporting view of it without its free-form details. Platform-side actions reach it through an outbox, so a briefly unavailable store loses nothing. Approval rules decide who may approve each kind of schema change: editors for creating tables, adding columns and creating indexes; admins for drops, restores, purges and ownership changes. Admins may raise a rule but never lower it below the default.

[Spec](../../specs/phase-1-foundation.md)

**Blocked by:** [05: Triggers, chats, invocations and schema change requests move](05-move-triggers-chats-approvals.md), [07: Roles and permissions on every route](07-roles-and-permissions.md)

**Status:** ready-for-agent

- [ ] Updating or deleting audit entries as the app's role, even through the store's owner, fails; a test proves it.
- [ ] An entry for a platform-side action written while the store is unavailable arrives once it's back, exactly once.
- [ ] The Audit log tab is admins-only and filters by actor, action and date, newest first.
- [ ] Adversarial test: audit entries' details are on the deny list, so `query_data` and `describe_data` can't read them; `write_data` can't insert, update or delete audit entries; the reporting view shows only action, actor, target and time.
- [ ] An editor can't approve a request that adds one column and drops another; only admins were notified of it.
- [ ] Lowering a rule below its default answers 422; raising it works and is recorded in the audit log.
- [ ] The Data page's Changes tab shows the role each approval needs and disables Approve for those who can't decide it.

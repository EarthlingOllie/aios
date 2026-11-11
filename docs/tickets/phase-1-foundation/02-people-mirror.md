# 02: People mirror

**What to build:** Each store gains a people mirror (`aios.people`): one row per person who is or was a member, with their id, names, email, role, and when they joined and left. The platform keeps it up to date itself: joining, leaving, renaming and role changes write the mirror row after the platform's own commit. "Made by" and "changed by" resolve from the store through the mirror, and people who have left show as former members. A repair sweep on the maintenance worker rebuilds the mirror from memberships and reports what it fixed.

This ticket also sets up the deny list: a few columns and tables are left out of the read-only and per-agent roles' grants, and so out of `query_data`, `describe_data` and the Tables view. People's emails in the mirror are the first entry; later tickets add audit log details, and later phases secret and credential columns. The read-write role behind `write_data` writes only in the `data` schema.

- Provisioning creates the mirror in a new store, and the store migration command creates it in existing ones; `make seed` fills it for dev data.

[Spec](../../specs/phase-1-foundation.md)

**Blocked by:** [Phase 0 · 02: Models split into a package by domain](../phase-0-refactor-and-guard-rails/02-models-package.md), [Phase 0 · 12: Delete an organisation with a grace period; remove unscoped routes](../phase-0-refactor-and-guard-rails/12-delete-organisation.md)

**Status:** done

- [x] Joining, leaving, renaming and role changes reach the store's people mirror.
  (Until roles arrive (Phase 1 · 07) a member's role is derived: admin for the creator, editor
  for everyone else. A role change marks the mirror changed (`aios.datastores.people.changed`),
  as joining, leaving, a rename and a new email do; the tests change the creator to stand in for
  the role endpoint that ticket adds.)
- [x] Lists and pages show who made and last changed each thing from the store, and "former member" for people who have left.
- [x] A new store and an existing store both have the mirror after provisioning or the store migration command.
- [x] A deleted or stale mirror row is rebuilt by the repair sweep, which reports what it fixed.
- [x] Adversarial test: `query_data` and `describe_data` can't read or list people's emails in the mirror, and `write_data` can't write outside the `data` schema.

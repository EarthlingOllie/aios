# 02: People mirror

**What to build:** Each store gains a people mirror (`aios.people`): one row per person who is or was a member, with their id, names, email, role, and when they joined and left. The platform keeps it up to date itself: joining, leaving, renaming and role changes write the mirror row after the platform's own commit. "Made by" and "changed by" resolve from the store through the mirror, and people who have left show as former members.

- Provisioning creates the mirror in a new store, and the store migration command creates it in existing ones; `make seed` fills it for dev data.

[Spec](../../specs/phase-1-foundation.md)

**Blocked by:** [Phase 0 · 02: Models split into a package by domain](../phase-0-refactor-and-guard-rails/02-models-package.md), [Phase 0 · 12: Delete an organisation with a grace period; remove unscoped routes](../phase-0-refactor-and-guard-rails/12-delete-organisation.md)

**Status:** ready-for-agent

- [ ] Joining, leaving, renaming and role changes reach the store's people mirror.
- [ ] Lists and pages show who made and last changed each thing from the store, and "former member" for people who have left.
- [ ] A new store and an existing store both have the mirror after provisioning or the store migration command.

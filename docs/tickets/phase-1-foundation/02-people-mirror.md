# 02: Private schemas, toolkit for moving domains, people mirror

**What to build:** Each store gains private schemas for the organisation's conversations, runs and configuration, reachable only by a new per-store `_app` login role that the platform's own code uses. The roles behind `query_data`, `describe_data` and the Data page's Tables view can neither read nor list them, so nobody outside a private chat or direct message can read it, even through SQL an agent writes; agents reach conversation content only through search. A reporting schema of views without conversation text is readable by agents. On top of this comes the shared toolkit every domain move uses (mirror writes, a backfill that can run twice, a verifier, per-domain read switches, a reconciler), proven first on the people mirror, which makes "made by" and "changed by" resolve from the store and shows people who have left as former members.

- Provisioning and the store migration command both create the private schemas, the `_app` role and its stored, encrypted password.
- The Tables view and `describe_data` list only `data` plus explicitly allowed tables.

[Spec](../../specs/phase-1-foundation.md)

**Blocked by:** [Phase 0 · 02: Models split into a package by domain](../phase-0-refactor-and-guard-rails/02-models-package.md), [Phase 0 · 12: Delete an organisation with a grace period; remove unscoped routes](../phase-0-refactor-and-guard-rails/12-delete-organisation.md)

**Status:** ready-for-agent

- [ ] The read-only and read-write store roles can neither read nor list the private schemas; adversarial test: `query_data` and `describe_data`, including system-catalogue queries and role-switching through session settings, return a permission error or nothing.
- [ ] The read-only role can read the reporting schema.
- [ ] Existing stores gain the `_app` role and private schemas through the store migration command.
- [ ] A backfill run twice leaves identical stores; the verifier reports any count or checksum mismatch.
- [ ] A domain's read switch flips both ways with no data lost while writes go to both databases.
- [ ] Joining, leaving, renaming and role changes reach the store's people mirror, and a deleted mirror row is rebuilt by the reconciler.
- [ ] Lists and pages show who made and last changed each thing from the store, and "former member" for people who have left.

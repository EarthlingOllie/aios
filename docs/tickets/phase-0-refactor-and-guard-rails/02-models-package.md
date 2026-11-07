# 02: Models split into a package by domain

**What to build:** split the platform models, today one module of over a thousand lines, into a
package with one module per domain. Developers can then see and change one domain at a time, and
phase 1 can move domains into the store one by one.

The domain modules are:
- identity and organisations;
- the store registry;
- agents and skills;
- workflows;
- triggers;
- approvals (schema change requests, keeping their class name);
- notifications;
- chats.

This is a wide, mechanical refactor, sequenced as expand → migrate → contract:
1. Make the package, re-exporting every name, with today's contents moved in whole.
2. Move the domains out, a few at a time.
3. Remove the leftover module.

Nothing changes in behaviour, schema, routes or settings.

[Spec](../../specs/phase-0-refactor-and-guard-rails.md)

**Blocked by:** None (can start immediately)

**Status:** ready-for-agent

- [ ] Every existing import of a model name keeps working, through the package's re-exports.
- [ ] The migration check reports no difference at every intermediate step and at the end.
- [ ] Each domain lives in its own module, and the leftover module is gone.
- [ ] No model module is over about 600 lines.
- [ ] The whole backend suite and the browser tests pass unchanged.

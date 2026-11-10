# 03: Agents, skills and workflows move to the store

**What to build:** Agents, skills and workflows, with all their versions, skill pins, tool settings, nodes, edges and layout, move into the organisation's store in one change that also deletes their old platform tables; dev data is rebuilt with `make seed`. Every agents, skills and workflows screen and endpoint works from the store, including archiving, versions, restore, the graph view and the editor. The store tables carry the archive fields from phase 0; invocable-by and limits arrive with ticket 11.

[Spec](../../specs/phase-1-foundation.md)

**Blocked by:** [02: People mirror](02-people-mirror.md), [Phase 0 · 08: Archive workflows and skills; remove every hard delete](../phase-0-refactor-and-guard-rails/08-archive-workflows-and-skills.md)

**Status:** ready-for-agent

- [ ] The agents, skills and workflows API and browser tests pass against the store.
- [ ] The platform database no longer holds agents, skills or workflows tables, and `make seed` creates them in each store.
- [ ] Versions pin skill versions and agent or workflow versions within the store; the no-cycles and nesting rules still hold.
- [ ] Archiving, unarchiving, restoring a version and saving without changes behave as before.
- [ ] Ids repeat safely across organisations: an organisation-scoping test shows no route reaches another organisation's agent, skill or workflow.

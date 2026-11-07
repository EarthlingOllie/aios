# 03: Agents, skills and workflows move to the store

**What to build:** Agents, skills and workflows, with all their versions, skill pins, tool settings, nodes, edges and layout, move into the organisation's store: expand, write to both databases, backfill, verify, then switch reads. Every agents, skills and workflows screen and endpoint works from the store, including archiving, versions, restore, the graph view and the editor. The store tables carry the archive fields from phase 0 and the new invocable-by and limits fields.

[Spec](../../specs/phase-1-foundation.md)

**Blocked by:** [02: Private schemas, toolkit for moving domains, people mirror](02-private-schemas-and-move-toolkit.md), [Phase 0 · 08: Archive workflows and skills; remove every hard delete](../phase-0-refactor-and-guard-rails/08-archive-workflows-and-skills.md)

**Status:** ready-for-agent

- [ ] The verifier reports zero differences for agents, skills and workflows in every store.
- [ ] The agents, skills and workflows API and browser tests pass with reads switched to the store.
- [ ] Versions pin skill versions and agent or workflow versions within the store; the no-cycles and nesting rules still hold.
- [ ] Archiving, unarchiving, restoring a version and saving without changes behave as before.
- [ ] Switching reads back to the platform works without data loss.
- [ ] Ids repeat safely across organisations: an organisation-scoping test shows no route reaches another organisation's agent, skill or workflow.

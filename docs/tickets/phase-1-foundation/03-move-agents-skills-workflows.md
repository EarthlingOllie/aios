# 03: Agents, skills and workflows move to the store

**What to build:** Agents, skills and workflows, with all their versions, skill pins, tool settings, nodes, edges and layout, move into the organisation's store in one change that also deletes their old platform tables; dev data is rebuilt with `make seed`. Every agents, skills and workflows screen and endpoint works from the store, including archiving, versions, restore, the graph view and the editor. The store tables carry the archive fields from phase 0; invocable-by and limits arrive with ticket 11.

[Spec](../../specs/phase-1-foundation.md)

**Blocked by:** [02: People mirror](02-people-mirror.md), [Phase 0 · 08: Archive workflows and skills; remove every hard delete](../phase-0-refactor-and-guard-rails/08-archive-workflows-and-skills.md)

**Status:** done

- [x] The agents, skills and workflows API and browser tests pass against the store.
  (Store revision 0009 makes the tables in every store; every agents, skills and workflows
  endpoint reads and writes them through a session on the asking organisation's store
  (`aios.auth.CurrentStore`). The API's shape is unchanged, so the browser tests, against their
  fake API, and the full-stack core loop, against the real one, pass as they were.)
- [x] The platform database no longer holds agents, skills or workflows tables, and `make seed` creates them in each store.
  (Platform revision 0060 drops them; runs, schedules, chat members, messages' authors and
  invocations keep the ids with no foreign key, and agent and workflow runs gain
  `organisation_id`. `make seed` makes them in the dev organisation's store.)
- [x] Versions pin skill versions and agent or workflow versions within the store; the no-cycles and nesting rules still hold.
- [x] Archiving, unarchiving, restoring a version and saving without changes behave as before.
- [x] Ids repeat safely across organisations: an organisation-scoping test shows no route reaches another organisation's agent, skill or workflow.
  (`test_organisation_scoping.py::test_ids_repeat_across_organisations_and_each_reaches_only_its_own`:
  two organisations whose agents, skills and workflows have the same ids each reach only their
  own through every route.)

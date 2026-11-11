# 01: Sources foundation: organisation-aware tool checks, Uploads source

**What to build:** Every organisation's store gains the idea of a **source**, starting with the one it already has: everything members upload now belongs to an "Uploads" source. Every document now has a required source. Members see a Source column and filter on the Documents tab, and a read-only Sources tab on the Data page listing Uploads. Nothing about uploading, downloading, deleting or searching changes.

At the same time the tool library learns to check settings against the organisation, when a version is saved, restored or run. This prepares the connector tool kind, and existing tools are unaffected.

[Spec](../../specs/phase-4-inflow-and-insight.md)

**Blocked by:** [Phase 0 · 12: Delete an organisation with a grace period; remove unscoped routes](../phase-0-refactor-and-guard-rails/12-delete-organisation.md), [Phase 1 · 03: Agents, skills and workflows move to the store](../phase-1-foundation/03-move-agents-skills-workflows.md)

**Status:** done

- [x] Every store has exactly one Uploads source, made when the store is provisioned. Development stores get it when rebuilt with `make seed`.
- [x] Every uploaded document belongs to its store's Uploads source, and a document can't be written without a source.
- [x] Uploading, downloading, deleting and searching documents behave exactly as before.
- [x] The Documents tab shows each document's source and can be filtered by source; the Data page has a Sources tab listing Uploads for every member.
- [x] Uploads can't be paused or removed: the API answers 409 and the UI offers no such action.
- [x] Every existing tool saves, restores and runs unchanged.
- [x] A tool with an organisation check refuses settings that belong to another organisation on save, restore and run, with a message naming the tool and the reason.

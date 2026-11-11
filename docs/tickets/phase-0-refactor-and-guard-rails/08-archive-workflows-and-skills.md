# 08: Archive workflows and skills; remove every hard delete

**What to build:** workflows and skills are archived on the same terms as agents. Every hard
delete of an agent, skill or workflow is removed.

**Archiving a workflow:**
- It starts nothing new: by hand, by schedule, by mention, or nested in a live workflow (which
  then refuses to start).
- Its schedules pause.
- Runs already going finish.
- It shows a read-only "Used by" list: workflows that nest it, its schedules, and chats.

**Archiving a skill:**
- It's always allowed, even while agents use it.
- Agent versions that pin it keep running.
- It can't be added to a new agent version.
- Agents whose current version uses it show a notice.
- Its "Used by" list shows those agents.

Both lists get an Archived filter.

**Removed:**
- the agent delete endpoint, with its dependents protocol and endpoint;
- the half of the dependents logic that cancelled and deactivated what depended on an agent;
- the agent delete dialog;
- the skill and workflow delete endpoints.

Deleting individual runs stays.

[Spec](../../specs/phase-0-refactor-and-guard-rails.md)

**Blocked by:** [07: Archive agents](07-archive-agents.md)

**Status:** done

- [x] For an archived workflow, starting it by hand, by schedule, by mention, and starting a live
  workflow that nests it are each refused with "archived".
- [x] An archived workflow's schedules pause, and runs already going finish.
- [x] An agent whose current version uses an archived skill still runs, while saving a new agent
  version that includes the skill is refused.
- [x] The agent page shows a notice for a skill it uses that's archived.
- [x] "Used by" lists for workflows and skills are shown read-only, and archiving is never
  blocked.
- [x] The Workflows and Skills lists have an Archived filter, and their default lists hide
  archived items.
- [x] No route deletes an agent, skill or workflow. Deleting a single run still works.
- [x] The dependents tests and the agent delete browser spec are removed, and the backend suite
  and full-stack test pass.

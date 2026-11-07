# 10: Export and import agents and workflows

**What to build:** Editors export an agent or workflow version as a file, and import it into another organisation.

- **What an export holds.** An agent's export holds its configuration and the exact skill versions it pins. A workflow's export holds its nested workflows, agents and skills, each exactly once.
- **What never travels:** data, memory, runs, triggers, conversations, credentials, provenance, and the built-in marker.
- **Importing.** It creates new, unconnected copies at version 1.
  - Clashing names get "(imported)", and skills are always copied.
  - A missing tool or connector leaves that version unrunnable, with the reason shown.
  - A dry run previews everything first.
- **Audit.** Exports and imports are recorded in the audit log.

[Spec](../../specs/phase-2-organisation-experience.md)

**Blocked by:** [Phase 1 · 03: Agents, skills and workflows move to the store](../phase-1-foundation/03-move-agents-skills-workflows.md), [Phase 1 · 08: Audit log and approval rules](../phase-1-foundation/08-audit-log-and-approval-rules.md)

**Status:** ready-for-agent

- [ ] Editors and admins can export and import. Participants get a 403.
- [ ] An exported file holds what the spec lists and nothing else, and an exported organisation assistant imports as an ordinary agent.
- [ ] A round trip between two organisations works, and editing either side never changes the other.
- [ ] Clashing names get "(imported)", then "(imported 2)". Skills are never matched to existing ones by name.
- [ ] An agent used twice in a workflow is exported once and created once, with both nodes pointing at it. Nested workflows import in full.
- [ ] An imported version needing a missing tool or connector says so on its page and refuses to start.
- [ ] A dry run reports what would be created, renamed and left unrunnable, and writes nothing.
- [ ] A file that isn't an export gets a 422, a file that's too large gets a 413, and nothing is created.
- [ ] Exports and imports are recorded in the audit log.

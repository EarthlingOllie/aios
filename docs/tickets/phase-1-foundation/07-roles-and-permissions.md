# 07: Roles and permissions on every route

**What to build:** Every member has a role: admin, editor or participant. Creators become admins and everyone else editors. Admins change roles on the Members tab and choose the role an invitation link grants; the last admin can't be demoted. One permission check, following the spec's table, guards every route, and the screens hide or disable what a role can't do. Editors build agents, skills, workflows and schedules; participants chat and start runs, and view and search documents, while only editors and admins upload or delete them. Participants' mentions are refused with an explanation until the mention guards ship.

[Spec](../../specs/phase-1-foundation.md)

**Blocked by:** [02: People mirror](02-people-mirror.md), [Phase 0 · 12: Delete an organisation with a grace period; remove unscoped routes](../phase-0-refactor-and-guard-rails/12-delete-organisation.md)

**Status:** ready-for-agent

- [ ] "Who am I" returns each organisation with the person's role.
- [ ] An admin changes a member's role on the Members tab, and role changes reach the store's people mirror.
- [ ] An invited person joins with the role their link granted.
- [ ] Demoting the last admin answers 409 "An organisation needs an admin".
- [ ] A parametrised API test covers every route against the three roles, matching the spec's permission table; a refused action answers 403 naming the action.
- [ ] Participants can view, search and download documents but get 403 uploading or deleting them.
- [ ] A participant's mention answers 403 with an explanation until ticket 11 ships.
- [ ] Browser tests show a participant's view, with edit controls hidden or disabled with an explanation.

# 10: Move every route and screen under the organisation (notifications get their organisation here)

**What to build:** every organisation-scoped API route and every signed-in screen moves under the
organisation, so each link names its organisation. Two tabs can then work in two organisations,
and shared links open the right one.

Covered here:
- the organisation page, invitations and settings;
- the store status;
- documents and search;
- data tables, schema changes and dropped objects;
- agents, skills and runs;
- workflows, their runs, and the scheduler;
- chats.

Notifications stay per person across organisations, and each carries its organisation's id and
slug, so it opens in the right one.

These routes stay unscoped:
- signing in;
- the development sign-in picker;
- the person's own profile;
- opening and accepting invitations;
- the tool library;
- the list of timezones.

The in-memory fake API and every browser spec move with the screens. The domains can move in
batches, each its own change, and each route's unprefixed form goes in the change that moves
it: no transition routes are kept.

[Spec](../../specs/phase-0-refactor-and-guard-rails.md)

**Blocked by:** [09: Organisation in the path](09-organisation-in-the-path.md)

**Status:** ready-for-agent

- [ ] Every screen listed above works under the organisation's slug.
- [ ] The generated scoping test covers every moved route, each answering 404 to a non-member.
- [ ] Only the routes listed as unscoped remain unscoped.
- [ ] Each notification carries its organisation's id and slug, and opening it goes to that
  organisation.
- [ ] Two tabs on two organisations work independently: a message posted in each lands in that
  tab's organisation.
- [ ] The fake API and every browser spec use organisation-prefixed links, and the browser tests
  pass.
- [ ] The full-stack test passes.

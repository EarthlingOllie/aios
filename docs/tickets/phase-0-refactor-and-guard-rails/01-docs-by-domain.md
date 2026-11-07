# 01: Docs by domain, and a slim README

**What to build:** replace the thousand-line README, which serves as the only spec, with a short
overview and a set of docs pages, one per domain. A developer can find out how one part of aios
works without reading everything.

- New pages: an index page, plus one each for accounts and organisations, stores, development,
  deploy, tracing, agents and skills, tools, workflows, chats, and triggers.
- Each README section moves to one page.
- Stale lines are fixed as they move.
- The README is then cut down to an overview, the architecture, a quick start and links.

Pages describe today's behaviour and use the glossary's words.

[Spec](../../specs/phase-0-refactor-and-guard-rails.md)

**Blocked by:** None (can start immediately)

**Status:** ready-for-agent

- [ ] A docs index page lists every page with one line each.
- [ ] Every heading of the old README exists on exactly one docs page. Nothing is lost or
  duplicated.
- [ ] Each stale line is fixed:
  - [ ] stores described as "one per user" (they're per organisation);
  - [ ] the reference to a plan "on the plan branch" is removed;
  - [ ] "and later a mention in a chat" is corrected;
  - [ ] "once runs can pause" is corrected.
- [ ] No page contradicts the code, and pages use glossary vocabulary, with "Organisation" on
  screen.
- [ ] The README is about 150 lines: product summary, architecture diagram and stack table, quick
  start, and links to the glossary, docs index, decision records and roadmap.
- [ ] Nothing remains only in the README that isn't also on a docs page.

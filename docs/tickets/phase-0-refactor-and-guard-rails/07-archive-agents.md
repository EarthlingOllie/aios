# 07: Archive agents

**What to build:** a member can archive an agent in one confirmation instead of deleting it.
Nothing is lost, and nothing new starts on it.

The confirmation shows a read-only "Used by" list:
- live workflows that run the agent at any depth;
- its schedules;
- the chats it's in;
- the data tables it will leave read-only, which stays empty until table ownership exists.

Archiving is never blocked by anything on that list.

**What an archived agent does:**
- It starts no new turns:
  - by hand;
  - by schedule;
  - by mention: the invocation fails at once, before the handoff, so no model call is made;
  - as part of a workflow whose pinned graph includes it at any depth.
- Turns and runs already going finish.
- Its schedules are paused, and stay paused after unarchiving until someone resumes them.
- It shows as archived among chat members, and drops out of mention suggestions.
- Its name stays reserved.

**Unarchiving** brings it back. Any member may archive and unarchive, as any member may delete
today. Phase 1's roles later give this to editors and admins.

The agents list gets an Archived filter, and the default list hides archived agents. Agents,
skills and workflows all gain archived fields here, and their soft-delete columns are dropped in
the same change; dev data is rebuilt with `make seed`. Workflows and skills use their archived
fields in the next ticket.

[Spec](../../specs/phase-0-refactor-and-guard-rails.md)

**Blocked by:** None (can start immediately)

**Status:** done

- [x] Archiving and unarchiving an agent work from its page, with an archived badge shown.
- [x] Starting a turn by hand, a schedule firing, and starting a workflow that includes the agent
  at any depth (a nested workflow at depth 2 included) are each refused with "archived", naming
  the agent.
- [x] Mentioning an archived agent posts the message, fails its invocation with "The agent
  <name> is archived", and makes no model call.
- [x] An archived agent shows as archived among chat members and isn't offered in mention
  suggestions. Unarchiving restores both.
- [x] A turn already going finishes, and a workflow run already past its start runs the agent's
  node.
- [x] Schedules are paused on archiving and stay paused after unarchiving.
- [x] Creating a new agent with an archived agent's name answers 409 naming the clash.
- [x] The "Used by" list matches what the old delete flow found as dependents (with tables empty
  for now), and archiving is never refused.
- [x] The Agents list's Archived filter shows archived agents, and the default list hides them.
- [x] The soft-delete columns are gone from agents, skills and workflows, and `make seed`
  rebuilds dev data.

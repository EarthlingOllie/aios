# 03: Split the largest modules

**What to build:** split the four largest non-model modules by sub-domain, so each change in
later phases touches a small file:
- the agents API: CRUD, versions, runs, and the run's chat stream;
- the chats API: CRUD, members, messages and invocations;
- workflow execution: validation, execution and steps;
- schema changes: spec, compile, apply and expiry.

Each API package still mounts one router, and public names are re-exported. These are moves only,
so each split can land as its own pull request.

[Spec](../../specs/phase-0-refactor-and-guard-rails.md)

**Blocked by:** None (can start immediately)

**Status:** done

- [x] Every route keeps the same method, path, request and response.
- [x] Background task names and the import paths used by tests are unchanged.
- [x] Each resulting module is about 600 lines or under. Where it isn't, the change explains why
  it's one cohesive unit.
- [x] The whole backend suite and the browser tests pass unchanged.

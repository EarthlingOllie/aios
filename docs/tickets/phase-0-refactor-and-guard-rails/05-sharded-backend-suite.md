# 05: Backend suite sharded, in random order, with one checks job

**What to build:** CI time for the backend stays roughly flat as tests are added.
- **Random order.** The suite runs in random order by default, with the seed printed, and every
  test that depended on another's data or on global counts is fixed.
- **Shards.** The suite is split across N CI jobs, each with its own database, Redis and storage,
  balanced on recorded test durations.
- **Checks once.** Lint, formatting and the migration check run once, in their own job.
- **One gate.** A single gate job passes only if every shard passed.
- **Durations.** A scheduled workflow, which can also be run by hand, refreshes the recorded
  durations.
- **Docs.** A new testing docs page covers running one shard locally and reproducing a seed.

[Spec](../../specs/phase-0-refactor-and-guard-rails.md)

**Blocked by:** None (can start immediately)

**Status:** ready-for-agent

- [ ] Three consecutive CI runs with different seeds pass.
- [ ] No test relies on another test's data, or counts every user, organisation or store.
- [ ] The slowest backend shard takes close to total time divided by N, plus setup.
- [ ] A new test file with no recorded duration still runs, in some shard.
- [ ] Lint, formatting and the migration check run once, in their own job, and shards don't
  repeat them.
- [ ] A failing shard fails the gate. Branch protection needs only the checks job and the gate.
- [ ] Changing the shard count is a one-line change.
- [ ] New test dependencies are pinned exactly, per the dependency policy.
- [ ] The testing docs page explains:
  - [ ] running one shard locally;
  - [ ] reproducing a seed;
  - [ ] running in file order;
  - [ ] refreshing durations.

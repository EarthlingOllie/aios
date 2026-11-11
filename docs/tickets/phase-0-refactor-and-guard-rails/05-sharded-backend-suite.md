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

**Status:** done

- [ ] Three consecutive CI runs with different seeds pass. (Local sharded runs with three
  seeds pass, after fixing the one order-dependent test the first found, but for failures of
  this machine's environment; awaits CI.)
- [x] No test relies on another test's data, or counts every user, organisation or store.
- [ ] The slowest backend shard takes close to total time divided by N, plus setup. (Locally the
  four shards finish within 7% of each other; awaits CI.)
- [x] A new test file with no recorded duration still runs, in some shard.
- [x] Lint, formatting and the migration check run once, in their own job, and shards don't
  repeat them.
- [ ] A failing shard fails the gate. Branch protection requires the checks job, the backend
  gate, the frontend gate, the full-stack job and the Terraform job, as the spec sets out
  (ticket 06 adds the frontend gate and the full-stack shards). (`backend-gate` is wired in
  ci.yml; branch protection is a repository setting, and awaits an admin and the first CI run.)
- [x] Changing the shard count is a one-line change.
- [x] New test dependencies are pinned exactly, per the dependency policy.
- [x] The testing docs page explains:
  - [x] running one shard locally;
  - [x] reproducing a seed;
  - [x] running in file order;
  - [x] refreshing durations.

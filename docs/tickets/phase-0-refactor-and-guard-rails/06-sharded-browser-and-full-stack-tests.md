# 06: Browser and full-stack tests sharded

**What to build:** the browser tests and the full-stack test run across several CI machines, and
a developer still gets one report.
- The build and unit tests run once.
- The browser tests run in N shards.
- The full-stack test runs in its own shards, each with its own stack.
- A gate per suite merges its shards' reports into one and fails if any shard failed.

[Spec](../../specs/phase-0-refactor-and-guard-rails.md)

**Blocked by:** [04: Full-stack test, with the scripted model as a selectable provider](04-full-stack-test.md)

**Status:** done

- [x] The build and unit tests run once, in their own job.
- [ ] The browser tests run in N shards, and the slowest takes close to total time divided by N,
  plus setup. (CI-only to confirm. Locally, 3 shards of the 329 tests took 208s, 177s and 170s.)
- [ ] A failing shard fails its gate, and the merged HTML report is downloadable. (CI-only to
  confirm. Locally, a deliberately failing shard exited 1 and its blob merged into one HTML
  report; `merge-reports` itself exits 0, so the gate checks the shards' result.)
- [x] Full-stack shards each bring up their own stack and share nothing. The default count is 1.
- [x] Each shard count is a one-line change.
- [x] The testing docs page explains running one browser test shard locally.

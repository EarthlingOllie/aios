# 06: Browser and full-stack tests sharded

**What to build:** the browser tests and the full-stack test run across several CI machines, and
a developer still gets one report.
- The build and unit tests run once.
- The browser tests run in N shards.
- The full-stack test runs in its own shards, each with its own stack.
- A gate per suite merges its shards' reports into one and fails if any shard failed.

[Spec](../../specs/phase-0-refactor-and-guard-rails.md)

**Blocked by:** [04: Full-stack test, with the scripted model as a selectable provider](04-full-stack-test.md)

**Status:** ready-for-agent

- [ ] The build and unit tests run once, in their own job.
- [ ] The browser tests run in N shards, and the slowest takes close to total time divided by N,
  plus setup.
- [ ] A failing shard fails its gate, and the merged HTML report is downloadable.
- [ ] Full-stack shards each bring up their own stack and share nothing. The default count is 1.
- [ ] Each shard count is a one-line change.
- [ ] The testing docs page explains running one browser test shard locally.

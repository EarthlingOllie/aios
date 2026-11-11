# 04: Full-stack test, with the scripted model as a selectable provider

**What to build:** one browser test that drives the core loop through the real API, workers,
database, Redis, storage and web front end:
1. A person mentions an agent in a chat.
2. The agent requests a schema change for a notes table.
3. The card shows the run waiting.
4. The person approves on the Data page.
5. The run resumes, writes a row and replies "Saved.", which appears in the chat.

To make this possible without outbound calls, a running API or worker can select the scripted
model as its model and handoff model, with deterministic fake embeddings, behind a new setting
that's off by default. The backend tests and the full-stack test share the same scripted model.
The test runs as its own CI job. Later tickets add scenarios here rather than build new harnesses.

[Spec](../../specs/phase-0-refactor-and-guard-rails.md)

**Blocked by:** None (can start immediately)

**Status:** done

- [x] With scripted models enabled, an API test runs a chat turn on a named scenario with no
  calls to model, embedding or tracing providers.
- [x] With the setting off, naming a scripted model fails the turn as an unknown provider, and
  nothing real is called.
- [x] The API refuses to start with scripted models enabled while secure cookies are on.
- [x] The backend tests' scripted model comes from the shared home, and the backend suite
  passes.
- [x] A Compose profile brings up the whole stack with no outbound calls.
- [ ] The core-loop test passes in CI. (Passes locally; awaits the first CI run.)
- [x] The core-loop test fails within its timeout when the provisioning worker is stopped.
- [ ] On failure, CI keeps the test report and the stack's logs. (Wired in ci.yml; awaits the first CI run.)

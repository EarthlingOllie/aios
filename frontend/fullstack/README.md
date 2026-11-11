# Full-stack tests

Browser tests against the real stack: the API, the ingest, workflow and provisioning workers,
Postgres, Redis, RustFS and the web front end, from Compose's `fullstack` profile. The model is
scripted (`backend/src/aios/testing/scenarios.py`), with fake embeddings, so nothing calls a
model, embedding or tracing provider and no API keys are needed.

```sh
# From the repository root: build and start the stack, and wait until it's ready (:8090).
docker compose --profile fullstack up --build --wait fullstack
# From frontend/: run the tests against it (FULLSTACK_URL points elsewhere).
pnpm test:fullstack
# Afterwards, its logs, and tearing it down (with its data).
docker compose --profile fullstack logs
docker compose --profile fullstack down -v
```

The stack shares the dev stack's database, Redis and storage services (and their ports), so stop
the dev stack first, or give one a different project name (`docker compose -p ...`).

- The API refuses to start with scripted models enabled while secure cookies are on.
- A scenario is a script of the model's turns: add one to `SCENARIOS` for each new story, and a
  test here that tells it, rather than a new harness. The backend tests script the same model
  (`aios.testing.fake_model`).
- A step that never comes (a worker stopped, say) fails the test within its timeout
  (`playwright.fullstack.config.ts`). CI runs the tests in shards (default 1), each on its own
  machine with its own stack, and keeps the merged report and each failing shard's stack logs.

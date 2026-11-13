# Testing

How the test suites run, locally and in CI. Setting up the services the backend tests need
(Postgres, Redis, RustFS) is in [Development](development.md#checks-and-tests).

## The backend suite

Run it from `backend/`:

```sh
uv run pytest
```

### Through the pooler

In CI the suite reaches every data store through PgBouncer in transaction mode, a service
container of each shard (`DATA_STORE_POOLER_ADDRESS=localhost:6432`), as Compose and AWS do, so a
store connection that leaned on session state (a prepared statement, a startup setting, a session
advisory lock) fails there. Locally, point `DATA_STORE_POOLER_ADDRESS` at a PgBouncer in front of
your Postgres (Compose's `pgbouncer`, on `localhost:6432`), or leave it empty to connect
directly. PgBouncer needs `auth_dbname` set to the platform database: otherwise it keeps its
auth query's connection on each store's database, and dropping a store waits on it.

### Random order

The tests run in random order ([pytest-randomly](https://github.com/pytest-dev/pytest-randomly)):
modules in a random order, and the tests in each module too. It's there to keep tests
independent, so a test must not rely on rows another test made, or count every user,
organisation or store in the database: scope what it checks to what it made itself.

Each run picks a seed and prints it in the header:

```
Using --randomly-seed=1234567890
```

It also seeds Python's `random` (and Faker's and NumPy's, if installed) with it before each test.

**Reproducing a seed.** Pass the seed back to run the same order again:

```sh
uv run pytest --randomly-seed=1234567890
```

In CI the seed is the workflow run's id, and every shard of the run uses it; it's in each shard
job's log, in the header. `--randomly-seed=last` reuses the previous local run's.

**Running in file order.** To rule the order out while chasing a failure:

```sh
uv run pytest -p no:randomly
```

### Shards

CI splits the suite into groups ([pytest-split](https://github.com/jerry-git/pytest-split)), one job
each, each with its own Postgres, PgBouncer, Redis and RustFS. Groups are balanced on the recorded duration
of each test, in `backend/.test_durations`. A test with no recorded duration, in a new file or
not, still runs: pytest-split counts it at the average duration and puts it in some group.

**Running one shard locally.** Name the number of shards and which one, with the seed of the run
to reproduce it exactly:

```sh
uv run pytest --splits 4 --group 2 --randomly-seed=1234567890
```

The groups are worked out from the test ids and durations alone (pytest-split's
`least_duration` algorithm, set in `pyproject.toml`), not the order, so the same `--splits`,
`--group` and `backend/.test_durations` select the same tests whatever the seed.

To run several shards at once locally, give each its own Postgres instance, as CI does, not
just its own database: `tests/test_db_roles.py` works on every store on the instance, so it
trips over another shard's.

**Changing the shard count.** It's one line in `.github/workflows/ci.yml`: `shards:` under the
`backend` job.

### Refreshing durations

The [Test durations](../.github/workflows/test-durations.yml) workflow runs the suite sharded,
records each test's duration, merges them into `backend/.test_durations` and opens a pull
request with the change. It runs weekly, and by hand from Actions > Test durations > Run
workflow. A pull request opened by the workflow doesn't start CI by itself (GitHub's rule for its
own token): close and reopen it, or push to it, to run the checks.

To refresh them locally instead, run the whole suite with:

```sh
uv run pytest --store-durations --clean-durations
```

which rewrites `backend/.test_durations` with the durations of the tests that ran
(`--clean-durations` drops tests since removed or renamed; without it they stay, which is
harmless, as pytest-split ignores them). Only the relative durations matter, so a slower machine
than CI's does no harm.

A test's id must be the same in every collection, or the shards can disagree on which of them
runs it: give parametrized cases explicit `ids` when their values are made at collection (a
timestamp, a random token).

## CI

`.github/workflows/ci.yml`:

| Job | What it does |
| --- | --- |
| `checks` | Ruff's lint and formatting, and `alembic check` (the models match the migrations), once. |
| `backend / shard N` | One group of the backend suite, from `backend-tests.yml`; the shards only run tests. |
| `backend-gate` | Passes only if every backend shard passed. |
| `frontend` | The frontend's build and unit tests, once. |
| `browser (N)` | One shard of the browser tests (`frontend/e2e/`). |
| `frontend-gate` | Passes only if the build, the unit tests and every browser shard passed; merges the shards' reports. |
| `fullstack-shard (N)` | One shard of the full-stack stories (the core loop, phase 2's chats) against the real stack: `frontend/fullstack/README.md`. |
| `fullstack` | Passes only if every full-stack shard passed; merges the shards' reports. |
| `terraform` | Formatting, validation and `terraform test`. |

Branch protection requires `checks`, `backend-gate`, `frontend-gate`, `fullstack` and `terraform`.
Requiring the gate rather than each shard keeps that list the same when the shard count changes.

**The escape suite** (`backend/tests/test_agent_logins.py`, its `test_escape_*` tests) tries every
known way out of an agent's database role, as the agent's login and through `query_data`
([Data stores](stores.md), Table ownership). It's an ordinary part of the backend suite, so it
runs in the shards and `backend-gate` requires it: a change to grants that opens a hole can't
merge. It needs the shards' real Postgres (and `DATABASE_ADMIN_URL`, to read role verifiers).

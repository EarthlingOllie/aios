from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


def libpq_conninfo(url: str, **parts: str) -> str:
    """A database URL (written for SQLAlchemy, like DATABASE_URL) as plain libpq connection info
    for psycopg, with any `parts` (e.g. `database`) replaced."""
    return make_url(url).set(drivername="postgresql", **parts).render_as_string(hide_password=False)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Public origin the browser uses (SPA and API share it; the SPA calls the API at /api/*).
    public_url: str = "http://localhost:5173"

    # The platform database, as the app's role (`aios_app`, created by `aios.db_roles`): the API,
    # the workers and migrations all connect as it.
    database_url: str = "postgresql+psycopg://aios_app:aios_app@localhost:5432/aios"
    # The provisioner's role (`aios_provisioner`: CREATEDB and CREATEROLE), which creates data
    # stores and migrates them (`aios.datastores.provision`). New stores are created on its
    # instance, which must be DATABASE_URL's: the app reaches them at DATABASE_URL's address
    # (they may know the instance by different names, as in Compose). On AWS only the
    # provisioning worker (and the deploy's store migration) has it.
    data_store_provisioner_url: str = (
        "postgresql+psycopg://aios_provisioner:aios_provisioner@localhost:5432/aios"
    )
    # Encrypts data stores' passwords on their `data_stores` rows. Any string: the encryption
    # key is derived from it, so a long random one (as on AWS) is a strong key. Changing it makes
    # existing stores' passwords unreadable.
    data_store_credentials_key: SecretStr = SecretStr("dev-insecure-change-me")
    # The database new stores are copied from, made by `aios.db_roles`: it holds pgvector (in
    # an `extensions` schema), which the provisioner, not being a superuser, can't install.
    data_store_template: str = "aios_store_template"
    # For each store's login roles: a query's longest run, the longest a session may sit idle in
    # a transaction (both in milliseconds, and defaults a session may change), and how many
    # connections the role may hold.
    data_store_statement_timeout: int = Field(default=30_000, ge=1)
    data_store_idle_in_transaction_timeout: int = Field(default=60_000, ge=1)
    #
    # Stores are reached through a connection pooler (PgBouncer, in transaction mode) at this
    # address, "host:port", as the app reaches it: e.g. Compose's `pgbouncer:5432`. Empty means
    # directly, at the store's own address. Only the app's store sessions go through it: the
    # platform database, and the provisioner, stay directly connected. Through it, a store
    # connection carries no session state from one transaction to the next: no prepared
    # statements, no settings at connection startup and no session advisory locks.
    data_store_pooler_address: str = ""
    # A backstop, not the budget: the pooler's own pools (per store database and role) decide how
    # many server connections a store gets. `store_session` pools one connection per (store,
    # role) in each process, plus four while busy (`aios.datastores.connections`), and a direct
    # connection (no pooler) counts against this. Past it, a connection is refused, which
    # answers 503 (try again).
    data_store_connection_limit: int = Field(default=100, ge=1)
    # The most a `query_data` call returns: rows, and bytes of them as JSON. A result past
    # either is cut short there, and says so (`truncated`).
    query_data_max_rows: int = Field(default=200, ge=1)
    query_data_max_bytes: int = Field(default=64 * 1024, ge=1)
    # The most rows a `write_data` call writes: inserted, upserted, updated or deleted. A call
    # past it is refused, and writes nothing.
    write_data_max_rows: int = Field(default=100, ge=1)
    # Uploaded files, in S3 (`aios.datastores.files`): each store's under `stores/<store id>/`.
    # The region comes from boto3's usual places (on AWS, the task's).
    data_store_files_bucket: str = "aios-data-local"
    # An S3-compatible server to use instead of AWS's (RustFS, locally), as the app reaches it.
    # Empty on AWS.
    data_store_files_endpoint: str = ""
    # That server's keys. Empty on AWS, where credentials come from boto3's usual places (the
    # task's role).
    data_store_files_access_key: str = ""
    data_store_files_secret_key: SecretStr = SecretStr("")
    # The same server as the browser reaches it, which download links are signed for (a link
    # names its host): e.g. http://localhost:9000 while the app reaches it at http://rustfs:9000.
    # Empty: the same as data_store_files_endpoint.
    data_store_files_public_endpoint: str = ""
    # How long a download link lasts, in seconds. Each is a bearer link (anyone holding it can
    # download the file until then), minted per click, so a minute is plenty.
    document_download_url_seconds: int = Field(default=60, ge=1, le=7 * 24 * 60 * 60)
    # Each store's storage cap (`aios.datastores.quota`): the data its tables hold (live rows,
    # measured with pgstattuple), and the bytes of its documents' files. At either, uploads and
    # `write_data`'s inserts and updates are refused; reads and deletes carry on.
    data_store_db_quota_bytes: int = Field(default=1024**3, ge=1)
    data_store_files_quota_bytes: int = Field(default=5 * 1024**3, ge=1)
    # How long (milliseconds) applying an approved schema change waits for each lock it needs,
    # e.g. on a table in use, before failing: so it can't stall the store's other queries.
    schema_change_lock_timeout: int = Field(default=5_000, ge=1)
    # How long (days) a dropped table or column is kept, for a member to restore, before it's
    # purged for good (`aios.datastores.dropped`).
    data_store_dropped_retention_days: float = Field(default=30, ge=0)
    # A schema change request nobody has decided after this many days expires, which counts as
    # a rejection: the run that asked carries on, told so. Checked every
    # `schema_change_expiry_check_seconds` by the provisioning worker's beat.
    schema_change_request_expiry_days: float = Field(default=7, gt=0)
    schema_change_expiry_check_seconds: float = Field(default=60 * 60, gt=0)
    # An organisation scheduled for deletion is deleted this many days later, unless its creator
    # cancels (`aios.organisations`); the provisioning worker's beat looks for those whose grace
    # period is over every `organisation_deletion_check_seconds`.
    organisation_deletion_grace_days: float = Field(default=14, ge=0)
    organisation_deletion_check_seconds: float = Field(default=10 * 60, gt=0)
    # Platform-side audit entries wait in the platform's outbox until the provisioning worker's
    # beat delivers them to their organisation's store, this often (`aios.audit.drain`).
    audit_drain_seconds: float = Field(default=10, gt=0)
    # The hour (UTC, 0-23) the provisioning worker's beat prunes runs and chat history past
    # each organisation's retention (`aios.retention`): a quiet one.
    retention_prune_hour: int = Field(default=3, ge=0, le=23)
    redis_url: str = "redis://localhost:6379/0"

    # Session cookie (Starlette SessionMiddleware, signed with itsdangerous).
    session_secret: SecretStr = SecretStr("dev-insecure-change-me")
    session_cookie: str = "aios_session"
    session_max_age: int = 60 * 60 * 24 * 14
    cookie_secure: bool = False
    # The sign-in page's "sign in as" picker (`aios.dev_auth`): any user, with no password. For
    # development only: Compose turns it on; production never does.
    dev_login_enabled: bool = False

    # Rate limits on signing in, registering and checking a signed-in user's current password
    # (`aios.ratelimits`), as `limits` strings ("<count>/<period>", e.g. "10/15 minutes"),
    # counted in Redis. Past one: 429, no lockout.
    login_rate_limit_per_email: str = "10/15 minutes"
    login_rate_limit_per_ip: str = "50/15 minutes"
    register_rate_limit_per_ip: str = "5/hour"
    current_password_rate_limit_per_user: str = "10/15 minutes"
    # How many proxies in front of the API append the address they saw to X-Forwarded-For: 1
    # behind the load balancer (or Compose's nginx), 0 when the API is reached directly. The
    # client's address, which the per-IP limits count, is the one the outermost proxy appended.
    trusted_proxy_count: int = Field(default=0, ge=0)

    # Inference. Model strings are LangChain "provider:model" names; API keys
    # (ANTHROPIC_API_KEY, VOYAGE_API_KEY) are read by the providers from the environment.
    llm_model: str = "anthropic:claude-sonnet-5-5"
    # The model must be able to output `aios.datastores.models.EMBEDDING_DIM` (1024)
    # dimensions; that size is fixed by the vector column, so a model with a different size
    # needs a migration too (in the data store tree).
    embedding_model: str = "voyageai:voyage-4"
    embedding_batch_size: int = 64
    # The handoff (`aios.handoff`): before a chat mention's run starts, one call to this model
    # (empty: `llm_model`, the deployment's default) briefs it on the chat and writes the
    # acknowledgement, over the chat's last `handoff_window_messages` messages or about
    # `handoff_window_tokens` tokens of them, whichever is less.
    handoff_model: str = ""
    # For the full-stack test only: `LLM_MODEL` and `HANDOFF_MODEL` may name a scripted model,
    # `scripted:<scenario>` (`aios.testing.scenarios`), and embeddings are a deterministic fake,
    # so nothing calls a provider. Off, a scripted model is an unknown provider. The API refuses
    # to start with it on while `COOKIE_SECURE` is (a production-like configuration).
    scripted_models_enabled: bool = False
    handoff_window_messages: int = Field(default=30, ge=1)
    handoff_window_tokens: int = Field(default=8_000, ge=1)

    # The usage ledger (`aios.usage`): estimated prices in pounds sterling, by model ("provider:
    # model", as `llm_model` and `embedding_model` name them), as a JSON object. A chat model's
    # are per million tokens of `input`, `output`, `cache_read` and `cache_write`, and per
    # `web_search` its provider runs; an embedding model's per million (estimated) tokens of
    # `input`; `sandbox`'s `per_second` of a sandbox session. A model with no price records a
    # blank cost. Covers the deployment's current models.
    price_table: dict[str, dict[str, float]] = {
        "anthropic:claude-sonnet-5-5": {
            "input": 2.40,
            "output": 12.00,
            "cache_read": 0.24,
            "cache_write": 3.00,
            "web_search": 0.008,
        },
        "voyageai:voyage-4": {"input": 0.05},
        "sandbox": {"per_second": 0.00004},
    }
    # Mention guards (`aios.guards`): how many agents and workflows one message may mention (a
    # 422 past it), and how many allowed mentions a person may make in an organisation, as a
    # `limits` string counted in Redis (a 429 past it).
    mentions_per_message: int = Field(default=5, ge=1)
    mention_rate_per_person: str = "60/minute"
    # Caps (`aios.limits`): a turn stops before a model call that would pass one, and so does a
    # workflow run's agent node. Admins override them for an organisation; agents and workflows
    # may lower them (and an admin raise them). Costs are in pounds sterling.
    turn_max_model_calls: int = Field(default=200, ge=1)
    turn_max_tokens: int = Field(default=5_000_000, ge=1)
    turn_max_cost: float = Field(default=25, gt=0)
    workflow_run_max_model_calls: int = Field(default=2_000, ge=1)
    workflow_run_max_cost: float = Field(default=100, gt=0)

    # Tracing agent runs to Langfuse (`aios.tracing`). Off unless both keys are set.
    langfuse_public_key: str = ""
    langfuse_secret_key: SecretStr = SecretStr("")
    langfuse_base_url: str = "http://localhost:3000"
    langfuse_environment: str = ""

    # Docling HybridChunker.
    chunk_tokenizer: str = "sentence-transformers/all-MiniLM-L6-v2"
    chunk_max_tokens: int = 512

    max_upload_bytes: int = 50 * 1024 * 1024
    # The largest file an import takes (`aios.api.exports`); a larger one is a 413.
    export_max_bytes: int = Field(default=5 * 1024 * 1024, ge=1)
    # Per-attempt budget for converting and chunking one document (Celery soft time limit).
    ingest_time_limit: int = 30 * 60
    # Budget for a whole workflow run, every node at every depth (`aios.workflows.execute_run`).
    workflow_run_time_limit: int = 2 * 60 * 60
    # A run fails once its worker's event loop has made no progress for this long: something
    # (a blocking call, CPU-bound work, a deadlock) has held the loop, so none of the run's nodes
    # can move. A node awaiting a slow model or tool call leaves the loop free, so it isn't
    # affected. Well over a dead worker's lease (`aios.workflows.STALE_AFTER`, 90s), so a node
    # can hold the loop for a minute or two and still finish (`aios.workflows._Heartbeat`).
    workflow_run_no_progress_limit: int = 5 * 60

    # Hybrid search.
    search_candidates: int = 40
    search_rrf_k: int = 60
    # How long each store's embedding sweep may spend embedding new search items (seconds) each
    # time it runs (`aios.search_index.embed_pending`, every `search_embed_interval_seconds`):
    # what's left waits for the next sweep, and is found by keyword meanwhile.
    search_embed_budget_seconds: float = Field(default=60, ge=0)
    search_embed_interval_seconds: float = Field(default=60, gt=0)

    # Jev, the prompt-injection classifier (`aios.jev`), that checks untrusted content before it
    # reaches a model. Its address (the server that answers `POST /v1/systemone`, hosted or
    # Compose's container) and bearer key. "fake" uses the scripted fake (`aios.jev.FakeJev`),
    # for development and `make seed`; empty means none is configured, so nothing can be
    # checked: documents wait unsearchable and checked tool results become errors (it fails
    # closed).
    jev_url: str = ""
    jev_api_key: SecretStr = SecretStr("")
    # The model name the request carries (Jev's spec requires one; laya-serve routes by language
    # for names it doesn't know).
    jev_model: str = "jev-latest"
    # A check gives up after this long (seconds), and the content counts as unchecked.
    jev_timeout_seconds: float = Field(default=3, gt=0)
    # Content is flagged when Jev's probability that it's a prompt injection reaches this.
    jev_threshold: float = Field(default=0.5, gt=0, le=1)
    # Bumped by hand when what we ask Jev changes in a way stored verdicts shouldn't outlive:
    # part of the version the re-check job compares (`aios.verdicts.recheck`).
    jev_policy_version: str = "1"
    # How often Jev's health is polled, and stored content awaiting a check (or checked by an
    # older version of Jev) is checked again (seconds).
    jev_health_interval_seconds: float = Field(default=30, gt=0)
    jev_sweep_interval_seconds: float = Field(default=300, gt=0)

    # Sandboxed Python (the `run_python` tool, `aios.sandbox`). With no backend the tool tells the
    # model it isn't available.
    sandbox_backend: str = ""
    # The egress proxy as sandboxes reach it (host:port). Empty = sandboxes get no network.
    sandbox_proxy_address: str = ""
    # Signs the proxy tokens that carry each session's allowed domains. Shared by the API (which
    # mints them) and the egress proxy (which checks them).
    sandbox_proxy_secret: SecretStr = SecretStr("")
    # A sandbox stops itself after this long without a call, or this long in total.
    sandbox_idle_seconds: int = 15 * 60
    sandbox_max_session_seconds: int = 2 * 60 * 60
    # How long to wait for a new sandbox to take calls.
    sandbox_start_timeout: int = 180
    # SANDBOX_BACKEND=fargate: one Fargate task per agent run (`aios.sandbox.fargate`). Subnets
    # and security groups are comma-separated.
    sandbox_cluster: str = ""
    sandbox_task_definition: str = ""
    sandbox_subnets: str = ""
    sandbox_security_groups: str = ""
    sandbox_container: str = "sandbox"
    # SANDBOX_BACKEND=dev: one shared runner for local development, e.g. docker compose's
    # `sandbox` service. Unisolated: never use it in production.
    sandbox_dev_url: str = "http://localhost:8081"
    sandbox_dev_token: str = ""

    # Schedules (`aios.scheduler`): each run service process (`aios.run_service`) polls for due
    # ones this often. Processes share the work safely (each schedule is claimed by one), so
    # turning it off only keeps schedules from firing at all.
    scheduler_enabled: bool = True
    scheduler_interval_seconds: float = 15

    # Event triggers (`aios.outbox`, `aios.dispatcher`). Run service processes drain stores
    # whose outbox has pending events when woken, and every `event_dispatch_poll_seconds`
    # regardless, `event_dispatch_batch` events at a time. The sweep (every
    # `event_sweep_interval_seconds`) stamps stores whose pending stamp was lost, and prunes
    # events dispatched more than `event_retention_days` ago. A chain of causes (a trigger's
    # run causing an event that fires another, ...) may be at most `chain_max_depth` long, and
    # each event trigger fires at most `event_trigger_rate_per_minute` times a minute.
    event_dispatch_poll_seconds: float = Field(default=5, gt=0)
    event_dispatch_batch: int = Field(default=100, ge=1)
    event_sweep_interval_seconds: float = Field(default=60, gt=0)
    event_retention_days: int = Field(default=14, ge=1)
    event_trigger_rate_per_minute: int = Field(default=60, ge=1)
    chain_max_depth: int = Field(default=8, ge=1)

    # The run service (`aios.run_service`): each process runs up to `run_service_concurrency`
    # items at once, `run_service_interactive_reserve` of them kept for people waiting in chat.
    # It's woken when work arrives, and polls every `run_service_idle_poll_seconds` regardless.
    # On a termination signal it stops claiming and lets its items finish for up to
    # `run_service_shutdown_grace_seconds`, then hands the rest back to the queue.
    run_service_concurrency: int = Field(default=50, ge=1)
    run_service_interactive_reserve: int = Field(default=10, ge=0)
    run_service_idle_poll_seconds: float = Field(default=5, gt=0)
    run_service_shutdown_grace_seconds: float = Field(default=25, ge=0)
    # The queue: a claimed item's lease lasts `queue_lease_seconds`, extended while it runs; an
    # item whose lease lapses (its process died) is claimed again, and after
    # `queue_max_attempts` claims it fails. An organisation has at most `queue_in_flight_cap`
    # items running at once unless an admin overrides it (soft: concurrent claimers can
    # overshoot it by the few each claims). A mention's turn should start within
    # `queue_chat_start_target_seconds`. The requeue sweep re-enqueues invocations left queued
    # without a work item for `queue_requeue_after_seconds`.
    queue_lease_seconds: float = Field(default=90, gt=0)
    queue_max_attempts: int = Field(default=3, ge=1)
    queue_in_flight_cap: int = Field(default=20, ge=1)
    queue_chat_start_target_seconds: float = Field(default=5, gt=0)
    queue_requeue_after_seconds: float = Field(default=60, gt=0)

    # A run's turns take turns through a lease on the run (`aios.turn_lease`): a turn holds it
    # for this long (seconds) at a time, refreshed by its heartbeat while it runs, so a process
    # that dies mid-turn frees its runs once this passes. A turn waits up to `turn_wait_seconds`
    # for the run's previous turn to end; past it, a run-page post answers 409 "This run is
    # busy".
    turn_lease_seconds: float = Field(default=90, gt=0)
    turn_wait_seconds: float = Field(default=120, ge=0)


@lru_cache
def get_settings() -> Settings:
    return Settings()

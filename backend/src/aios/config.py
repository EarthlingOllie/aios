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

    # Public origin the browser uses (SPA and API share it; the SPA calls /api/* and /login).
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
    # The connection limit's budget: `store_session` pools up to 3 connections per (store, role)
    # in each process (`pool_size=1, max_overflow=2`), so a role can be asked for 3 x the
    # processes that use it. On ECS both roles are used by the 2 API tasks and the workflow
    # worker's 2 processes (the data tools), so 12; the read-write one also by the ingest
    # worker's 2 (one ingest, so one connection, each), so 14, and the read-only one by the
    # provisioning worker (measuring one store at a time), so 13. A rolling deploy starts the new
    # tasks before stopping the old, which doubles that: 28 at most. Keep it at least that when
    # adding processes. Past it, a connection is refused, which answers 503 (try again).
    data_store_connection_limit: int = Field(default=30, ge=1)
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
    # The most a `query_data` call returns: rows, and bytes of them as JSON. A result past
    # either is cut short there, and says so (`truncated`).
    query_data_max_rows: int = Field(default=200, ge=1)
    query_data_max_bytes: int = Field(default=64 * 1024, ge=1)
    # The most rows a `write_data` call writes: inserted, upserted, updated or deleted. A call
    # past it is refused, and writes nothing.
    write_data_max_rows: int = Field(default=100, ge=1)
    # Each store's storage cap (`aios.datastores.quota`): its database's size
    # (`pg_database_size`), and the bytes of its documents' files. At either, uploads and
    # `write_data`'s inserts and updates are refused; reads and deletes carry on.
    data_store_db_quota_bytes: int = Field(default=1024**3, ge=1)
    data_store_files_quota_bytes: int = Field(default=5 * 1024**3, ge=1)
    redis_url: str = "redis://localhost:6379/0"

    # Session cookie (Starlette SessionMiddleware, signed with itsdangerous).
    session_secret: SecretStr = SecretStr("dev-insecure-change-me")
    session_cookie: str = "aios_session"
    session_max_age: int = 60 * 60 * 24 * 14
    cookie_secure: bool = False

    github_client_id: str = ""
    github_client_secret: SecretStr = SecretStr("")

    # Inference. Model strings are LangChain "provider:model" names; API keys
    # (ANTHROPIC_API_KEY, VOYAGE_API_KEY) are read by the providers from the environment.
    llm_model: str = "anthropic:claude-sonnet-5-5"
    # The model must be able to output `aios.datastores.models.EMBEDDING_DIM` (1024)
    # dimensions; that size is fixed by the vector column, so a model with a different size
    # needs a migration too (in the data store tree).
    embedding_model: str = "voyageai:voyage-4"
    embedding_batch_size: int = 64

    # Tracing agent runs to Langfuse (`aios.tracing`). Off unless both keys are set.
    langfuse_public_key: str = ""
    langfuse_secret_key: SecretStr = SecretStr("")
    langfuse_base_url: str = "http://localhost:3000"
    langfuse_environment: str = ""

    # Docling HybridChunker.
    chunk_tokenizer: str = "sentence-transformers/all-MiniLM-L6-v2"
    chunk_max_tokens: int = 512

    max_upload_bytes: int = 50 * 1024 * 1024
    # Per-attempt budget for converting + embedding one document (Celery soft time limit).
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

    # Schedules (`aios.scheduler`): each API process polls for due ones this often. Replicas share
    # the work safely (each schedule is claimed by one), so turning it off is only needed to keep
    # a process from running agents, e.g. one serving requests that must stay light.
    scheduler_enabled: bool = True
    scheduler_interval_seconds: float = 15
    # Most schedules a process fires at once. An agent's runs its whole turn in the process, next
    # to the chat it serves, so this keeps a burst (many sharing `0 9 * * *`) from crowding chat
    # out of the database pool and threadpool. A process with no free slot claims nothing, which
    # leaves what's due for another replica or a later tick.
    scheduler_max_concurrent_runs: int = Field(default=5, ge=1)

    @property
    def oauth_redirect_uri(self) -> str:
        return f"{self.public_url.rstrip('/')}/auth/callback"


@lru_cache
def get_settings() -> Settings:
    return Settings()

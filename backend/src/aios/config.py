from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Public origin the browser uses (SPA and API share it; the SPA calls /api/* and /login).
    public_url: str = "http://localhost:5173"

    database_url: str = "postgresql+psycopg://aios:aios@localhost:5432/aios"
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
    # The model must be able to output models.EMBEDDING_DIM (1024) dimensions; that size is
    # fixed by the vector column, so a model with a different size needs a migration too.
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

    @property
    def oauth_redirect_uri(self) -> str:
        return f"{self.public_url.rstrip('/')}/auth/callback"


@lru_cache
def get_settings() -> Settings:
    return Settings()

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

    # Inference. Model strings are Pydantic AI "provider:model" names; API keys
    # (ANTHROPIC_API_KEY, VOYAGE_API_KEY) are read by the providers from the environment.
    llm_model: str = "anthropic:claude-sonnet-5-5"
    # The model must be able to output models.EMBEDDING_DIM (1024) dimensions; that size is
    # fixed by the vector column, so a model with a different size needs a migration too.
    embedding_model: str = "voyageai:voyage-4"
    embedding_batch_size: int = 64

    # Docling HybridChunker.
    chunk_tokenizer: str = "sentence-transformers/all-MiniLM-L6-v2"
    chunk_max_tokens: int = 512

    max_upload_bytes: int = 50 * 1024 * 1024
    # Per-attempt budget for converting + embedding one document (Celery soft time limit).
    ingest_time_limit: int = 30 * 60

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
    # SANDBOX_BACKEND=lambda (aios.sandbox.aws_lambda): the sandbox function, and the S3 bucket
    # that keeps each session's /work between calls.
    sandbox_lambda_function: str = ""
    sandbox_work_bucket: str = ""
    # The most a session's /work may hold, unpacked. A call that leaves more isn't saved.
    sandbox_work_limit_mb: int = 512

    @property
    def oauth_redirect_uri(self) -> str:
        return f"{self.public_url.rstrip('/')}/auth/callback"


@lru_cache
def get_settings() -> Settings:
    return Settings()

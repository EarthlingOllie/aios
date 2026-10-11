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
    # SANDBOX_BACKEND=gvisor: one gVisor container per agent run, started by the broker on a
    # sandbox host (`aios.sandbox.gvisor`, `aios.sandbox.broker`). The API starts sessions at
    # SANDBOX_BROKER_URL (e.g. the brokers' internal load balancer), then calls the broker that
    # answered directly. With gVisor, SANDBOX_PROXY_ADDRESS is `egress-proxy:3128`.
    sandbox_broker_url: str = ""
    # Shared by the API and the brokers; every broker call needs it.
    sandbox_broker_token: SecretStr = SecretStr("")
    # Broker only. The URL the API should use for this host's sessions. Empty = http://<the
    # instance's private IP, from the EC2 metadata service>:SANDBOX_BROKER_PORT.
    sandbox_broker_advertise_url: str = ""
    sandbox_broker_port: int = 8090
    # Broker only. The container runtime for sandboxes: `runsc` (gVisor). `runc` works without
    # gVisor installed but doesn't isolate anything: local development only.
    sandbox_docker_runtime: str = "runsc"
    # Broker only. Images must already be on the host: the broker never pulls. The egress proxy
    # runs from an image with the `aios` package (the api or broker image).
    sandbox_image: str = "aios-sandbox:local"
    sandbox_proxy_image: str = "aios-broker:local"
    # Broker only. The internal (no route out) Docker network sandboxes and the proxy share.
    sandbox_network: str = "aios-sandbox"
    sandbox_network_subnet: str = "172.30.0.0/16"
    # Broker only. Per sandbox: memory (MB, including the tmpfs /work and /tmp), CPUs, processes,
    # and the size of /work and /tmp (MB). And how many sandboxes one host runs at most.
    sandbox_memory_mb: int = 1024
    sandbox_cpus: float = 1.0
    sandbox_pids_limit: int = 256
    sandbox_work_mb: int = 512
    sandbox_tmp_mb: int = 64
    sandbox_max_sessions: int = 50

    @property
    def oauth_redirect_uri(self) -> str:
        return f"{self.public_url.rstrip('/')}/auth/callback"


@lru_cache
def get_settings() -> Settings:
    return Settings()

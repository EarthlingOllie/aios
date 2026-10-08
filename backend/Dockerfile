# syntax=docker/dockerfile:1.7
# Targets: `api` (FastAPI/Uvicorn, also used for migrations) and `worker` (Celery + Docling).

FROM ghcr.io/astral-sh/uv:0.11.32 AS uv

FROM python:3.13-slim-bookworm AS base
COPY --from=uv /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"
WORKDIR /app
RUN useradd --create-home --uid 1000 app

# ---------- api ----------
FROM base AS api
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-dev --no-install-project
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-editable
USER app
EXPOSE 8000
CMD ["uvicorn", "aios.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]

# ---------- worker ----------
FROM base AS worker
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*
# Same pins as uv.lock, but torch/torchvision come from PyTorch's CPU-only index and the
# CUDA runtime packages (several GB that a CPU worker never uses) are dropped.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv export --locked --no-dev --no-hashes --no-emit-project --extra ingest \
      | grep -vE '^(nvidia-|cuda-|triton==)' > /tmp/requirements.txt \
    && uv venv /app/.venv \
    && uv pip install --python /app/.venv --torch-backend cpu -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv uv pip install --python /app/.venv --no-deps .

# Bake Docling's layout/table models and the chunker tokenizer into the image so
# workers never download at runtime.
ENV HF_HOME=/opt/hf \
    DOCLING_ARTIFACTS_PATH=/opt/docling-models
ARG CHUNK_TOKENIZER=sentence-transformers/all-MiniLM-L6-v2
RUN docling-tools models download -o "$DOCLING_ARTIFACTS_PATH" \
    && python -c "from transformers import AutoTokenizer; AutoTokenizer.from_pretrained('${CHUNK_TOKENIZER}')" \
    && chmod -R a+rX /opt/hf /opt/docling-models
ENV HF_HUB_OFFLINE=1
USER app
CMD ["celery", "-A", "aios.worker", "worker", "--loglevel=INFO", "--concurrency=2"]

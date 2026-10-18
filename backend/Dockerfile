# syntax=docker/dockerfile:1.7
# Targets: `api` (FastAPI/Uvicorn, also used for migrations and the egress proxy), `worker`
# (Celery + Docling) and `sandbox` (runs agent-written Python, see aios.sandbox).

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
# torch/torchvision are locked to PyTorch's CPU-only index (see pyproject.toml).
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-dev --no-install-project --extra ingest
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-editable --extra ingest

# Bake Docling's models and the chunker tokenizer into the image so workers never download
# at runtime. Only the models the default DocumentConverter in aios.ingest runs: layout, table
# structure and OCR (auto picks RapidOCR on torch). The CLI's default set also pulls
# code_formula and picture_classifier, which are off in that pipeline and run to several GB.
ENV HF_HOME=/opt/hf \
    DOCLING_ARTIFACTS_PATH=/opt/docling-models
ARG CHUNK_TOKENIZER=sentence-transformers/all-MiniLM-L6-v2
RUN docling-tools models download -o "$DOCLING_ARTIFACTS_PATH" layout tableformer rapidocr \
    && python -c "from transformers import AutoTokenizer; AutoTokenizer.from_pretrained('${CHUNK_TOKENIZER}')" \
    && chmod -R a+rX /opt/hf /opt/docling-models
ENV HF_HUB_OFFLINE=1
USER app
CMD ["celery", "-A", "aios.worker", "worker", "--loglevel=INFO", "--concurrency=2"]

# ---------- sandbox ----------
# Runs agent-written code. Only the runner and the pinned libraries in sandbox/requirements.txt:
# no app code and no app dependencies. Run it with a read-only root filesystem; /work and /tmp
# are the only paths that need to be writable.
FROM python:3.13-slim-bookworm AS sandbox
COPY --from=uv /uv /bin/
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=sandbox/requirements.txt,target=/tmp/requirements.txt \
    uv pip install --system --require-hashes -r /tmp/requirements.txt \
    && rm /bin/uv
# The code runs as this user (the runner drops to it when started as root).
RUN useradd --uid 1001 --no-create-home --home-dir /work sandbox \
    && mkdir /work && chown sandbox:sandbox /work
COPY src/aios/sandbox/runner.py /opt/sandbox/runner.py
WORKDIR /work
CMD ["python", "/opt/sandbox/runner.py", "serve"]

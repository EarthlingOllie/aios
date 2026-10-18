# syntax=docker/dockerfile:1.7
# Claude Code inside the compose network, for debugging the running stack (see README,
# "Debug with Claude Code"). The repo is bind-mounted at /workspace; nothing from it is baked in.

FROM ghcr.io/astral-sh/uv:0.11.32 AS uv
FROM node:22.22-bookworm-slim AS node
FROM docker:29.8.2-cli AS docker

FROM python:3.13-slim-bookworm
COPY --from=uv /uv /uvx /bin/
COPY --from=node /usr/local/bin/node /usr/local/bin/
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
# Read-only access to the other containers' logs and state, through the docker-proxy service.
COPY --from=docker /usr/local/bin/docker /usr/local/bin/
COPY --from=docker /usr/local/libexec/docker/cli-plugins/docker-compose /usr/local/libexec/docker/cli-plugins/

RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && ln -s ../lib/node_modules/corepack/dist/corepack.js /usr/local/bin/corepack \
    && corepack enable \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        git curl ca-certificates jq less procps ripgrep postgresql-client redis-tools \
    && rm -rf /var/lib/apt/lists/*

# Pinned like everything else (at least a week old, see "Dependency policy").
ARG CLAUDE_CODE_VERSION=2.1.293
RUN npm install -g --no-fund --no-audit "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && npm cache clean --force

# Claude Code won't skip permission prompts as root, and files it writes to the bind-mounted
# repo should belong to you: match your uid if it isn't 1000 (`--build-arg UID=$(id -u)`).
ARG UID=1000
RUN useradd --create-home --uid "${UID}" dev \
    && git config --system safe.directory /workspace

# Managed memory: Claude Code loads this in every session, whatever the working directory.
COPY CLAUDE.md /etc/claude-code/CLAUDE.md
COPY entrypoint.sh /usr/local/bin/entrypoint.sh

# Linux builds of the backend venv and frontend node_modules live outside the repo (volumes),
# so they don't clobber the ones on your host.
ENV PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/home/dev/.venv \
    UV_CACHE_DIR=/home/dev/.cache/uv \
    CLAUDE_CONFIG_DIR=/home/dev/.claude \
    COREPACK_ENABLE_DOWNLOAD_PROMPT=0
WORKDIR /workspace
ENTRYPOINT ["entrypoint.sh"]
CMD ["claude"]

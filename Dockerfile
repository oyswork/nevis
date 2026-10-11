FROM python:3.14-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

# Dependencies first so the layer is cached across source changes.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable


# Embedding and reranker models (~150 MB), baked in so the api starts without network access.
# Downloaded through a build cache, so a source change doesn't fetch them again; the final
# copy is the whole HF-hub layout (its symlinks are relative, so it survives being moved).
FROM builder AS models
RUN --mount=type=cache,target=/tmp/model-download \
    NEVIS_MODEL_CACHE_DIR=/tmp/model-download /app/.venv/bin/python -m nevis.embeddings \
    && mkdir /opt/models && cp -a /tmp/model-download/. /opt/models/


# Development image for docker compose: dependencies (dev ones included) but no code. Compose
# mounts the repository at /app, so the venv lives outside it and src/ is put on the path.
FROM python:3.14-slim AS dev

COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH=/app/src \
    PYTHONDONTWRITEBYTECODE=1 \
    NEVIS_MODEL_CACHE_DIR=/opt/models \
    HF_HUB_OFFLINE=1

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-install-project
COPY --from=models /opt/models /opt/models


# Production reverse proxy: Caddy (automatic HTTPS) plus the rate-limit module, which isn't in
# the stock build, with deploy/caddy baked in. The module has no recent tag: pinned to a commit.
FROM caddy:2.11.7-builder AS proxy-build
RUN xcaddy build --with github.com/mholt/caddy-ratelimit@5625512f24f6f59d6f64fb3aafe5eecff0b286db

FROM caddy:2.11.7 AS proxy
COPY --from=proxy-build /usr/bin/caddy /usr/bin/caddy
COPY deploy/caddy /etc/caddy


# Production image: the default build target.
FROM python:3.14-slim AS runtime

RUN useradd --system --uid 1000 app
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --from=models --chown=app:app /opt/models /opt/models
ENV PATH="/app/.venv/bin:$PATH" \
    NEVIS_MODEL_CACHE_DIR=/opt/models \
    HF_HUB_OFFLINE=1

USER app
EXPOSE 8000
CMD ["uvicorn", "--factory", "nevis.main:create_app", "--host", "0.0.0.0", "--port", "8000"]

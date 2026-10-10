# Nevis Search API

FastAPI + raw asyncpg + Postgres 16 with pgvector. Schema managed declaratively by Atlas.
Assignment spec: `task.md`.

## Commands

- Install: `uv sync`. Atlas runs only in Docker (the compose `migrate` service); no local CLI.
- Test infrastructure (start before `pytest`; isolated from the dev stack, port 55432, tmpfs):
  `docker compose -f docker-compose.yml -f docker-compose.test.yml up -d --wait migrate`.
  Re-run it after editing `schema/schema.sql`; tear down with the same `-f` flags and `down`.
- Test (80% coverage gate): `uv run pytest`. If integration tests are selected and the test
  database isn't reachable, named `*_test` and migrated, pytest aborts before running anything.
  `uv run pytest -m unit` needs no infrastructure.
- Type check (strict): `uv run mypy`
- Lint / format: `uv run ruff check --fix . && uv run ruff format .`
- Full stack: `docker compose up --build` → http://localhost:8000/docs
- Dev server with reload: `docker compose up -d migrate`, then
  `NEVIS_DATABASE_URL=postgresql://nevis:nevis@localhost:5432/nevis uv run uvicorn --factory nevis.main:create_app --reload`

Run `pytest`, `mypy` and `ruff` before calling a change done.

## Schema changes

`schema/schema.sql` is the single source of truth: Atlas applies it declaratively (no migration
files) and omymodels generates the row models from it.

1. Edit `schema/schema.sql`.
2. `uv run python scripts/generate_models.py` → regenerates `src/nevis/db_models.py`
   (never edit it by hand; `tests/test_db_models.py` fails if it's stale).
3. `docker compose run --rm migrate` applies the change to the local database.

Gotchas:
- Write `timestamp with time zone`, not `timestamptz`: omymodels doesn't map the alias.
- Postgres extensions (e.g. `vector`) are an Atlas Pro feature. Create them outside Atlas, in
  `docker/initdb/` (used by both stacks), in both the app and `atlas_dev` databases.
- Atlas connection settings live in `schema/atlas.hcl` (env `compose`), with URLs from
  `ATLAS_URL` / `ATLAS_DEV_URL`; `docker-compose.test.yml` overrides only those env vars.
- `docker/initdb/` scripts only run on a fresh volume (`docker compose down -v`).

## Conventions

- mypy runs in `strict` mode with the pydantic plugin. Don't use `Any` or `# type: ignore` without a comment saying why.
- Build the app with `create_app(settings)`. Never read env vars at import time.
- Configuration comes only from `nevis.config.Settings` (`NEVIS_*` env vars). `NEVIS_DATABASE_URL`
  must be a plain `postgresql://` DSN; asyncpg rejects driver-qualified schemes.
- Database access is raw SQL through the asyncpg pool (`PoolDep`). Validate rows into the
  generated `nevis.db_models` classes; request/response models live in each feature's `schemas.py`.
- Inject the pool, not a connection: a connection dependency is acquired before FastAPI rejects
  an invalid body. Use `pool.fetchrow(...)` for single statements, `pool.acquire()` for transactions.
- Mark tests `unit` (no external services) or `integration` (needs Postgres).
- Add dependencies with `uv add` / `uv add --dev` and commit `uv.lock`.

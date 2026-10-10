# Nevis Search API

FastAPI + raw asyncpg + Postgres 16, with a Meilisearch index derived from Postgres for search
(`decisions.md`). Schema in `schema/schema.sql`; Atlas generates and applies versioned migrations
from it. Index settings live in `src/nevis/search/indexes.py`; `search-setup` applies them.
Assignment spec: `task.md`.

## Commands

Everyday commands are `just` recipes (`just` lists them; see `justfile`).

- Install: `uv sync`. Atlas runs only in Docker (the compose `migrate` / `migrate-diff`
  services); no local CLI.
- Whole system in Docker: `just up` → http://localhost:8000/docs. The api runs the `dev` image
  stage with `src/` mounted and `--reload`; `just logs`, `just down`.
- Infra only, api on the host (to debug it): `just infra`, then `just run` (or your debugger
  with `NEVIS_DATABASE_URL=postgresql://nevis:nevis@localhost:5432/nevis`,
  `NEVIS_MEILISEARCH_URL=http://localhost:7700`, `NEVIS_MEILISEARCH_API_KEY=nevis-dev-master-key`).
- Dev data lives in `.data/postgres` and `.data/meilisearch` (bind mounts), so it survives
  `just down`; `just clean` deletes it. Embedding models are cached in the `huggingface-cache`
  volume. After changing index settings, `just search-setup` (safe to re-run).
- Tests in Docker (80% coverage gate): `just test [pytest args]`. Uses the separate test stack
  (`docker-compose.test.yml`: ports 55432 and 57700, tmpfs) and runs pytest in a `tests`
  container with the repository mounted.
- Tests on the host: `just test-infra` (re-run after adding a migration), then `uv run pytest`.
  If integration tests are selected and the test database isn't reachable, named `*_test` and
  migrated, or Meilisearch isn't reachable and set up, pytest aborts before running anything.
  `uv run pytest -m unit` needs no infrastructure. URLs default to localhost:55432 and
  localhost:57700 (`NEVIS_TEST_DATABASE_URL`, `NEVIS_TEST_MEILISEARCH_URL`).
- Lint, format check, type check (strict): `just check`; fix with
  `uv run ruff check --fix . && uv run ruff format .`
- Don't use `docker compose up --wait migrate` (or `search-setup`): `--wait` reports a one-shot
  as failed even when it exits 0. Wait for `db` / `meilisearch`, then `docker compose run --rm`.

Run `just test` and `just check` before calling a change done.

## Schema changes

`schema/schema.sql` is the single source of truth (see `decisions.md`). Atlas diffs it against
the migration history to generate versioned migrations in `schema/migrations/`, and omymodels
generates the row models from it. Commit all three together.

1. Edit `schema/schema.sql`.
2. `just migration <name>` → writes `schema/migrations/<timestamp>_<name>.sql`, updates
   `atlas.sum`, regenerates `src/nevis/db_models.py` and lists the files to commit together.
   Review the SQL (Atlas generates drops too). It reports "synced" if there is nothing to do.
   Never edit `db_models.py` by hand (`just models` regenerates it alone;
   `tests/test_db_models.py` fails if it's stale).
3. `just migrate` applies pending migrations to the dev database.

Gotchas:
- Write `timestamptz`, not `timestamp with time zone`: the generator maps `timestamptz` to
  `pydantic.AwareDatetime` (naive datetimes are rejected), but the DDL parser reduces the long
  spelling to plain `timestamp`, which becomes a naive `datetime.datetime`.
- Postgres extensions (e.g. `vector`) are an Atlas Pro feature. Create them outside Atlas, in
  `docker/initdb/` (used by both stacks), in both the app and `atlas_dev` databases.
- Never edit a migration that has been applied anywhere; add a new one. If you must edit an
  unapplied one, run `docker compose run --rm --entrypoint /atlas migrate-diff migrate hash
  --env compose` afterwards, or `migrate apply` rejects the directory (checksum in `atlas.sum`).
- Applied versions are recorded in `public.atlas_schema_revisions`; `migrate apply` refuses a
  database that has tables but no history (`--baseline <version>` adopts one).
- Atlas connection settings live in `schema/atlas.hcl` (env `compose`), with URLs from
  `ATLAS_URL` / `ATLAS_DEV_URL`; `docker-compose.test.yml` overrides only those env vars.
- `docker/initdb/` scripts only run on an empty data directory (`just clean` for the dev
  database; the test database is in-memory and starts empty every time).

## Conventions

- mypy runs in `strict` mode with the pydantic plugin. Don't use `Any` or `# type: ignore` without a comment saying why.
- Build the app with `create_app(settings)`. Never read env vars at import time.
- Configuration comes only from `nevis.config.Settings` (`NEVIS_*` env vars). `NEVIS_DATABASE_URL`
  must be a plain `postgresql://` DSN; asyncpg rejects driver-qualified schemes.
- Database access is raw SQL through the asyncpg pool (`PoolDep`). Validate rows into the
  generated `nevis.db_models` classes; request/response models live in each feature's `schemas.py`.
- Inject the pool, not a connection: a connection dependency is acquired before FastAPI rejects
  an invalid body. Use `pool.fetchrow(...)` for single statements, `pool.acquire()` for transactions.
- Changes that search must see enqueue an outbox job in the same transaction
  (`nevis.outbox.repository.enqueue`); the background worker runs it. Job handlers must be
  idempotent (a job can run more than once). Add a kind to `OutboxKind` and to
  `outbox_kind_check` in `schema.sql` together.
- Mark tests `unit` (no external services) or `integration` (needs Postgres and Meilisearch).
  Test apps never run the worker in the background: run jobs with `OutboxWorker.run_once()`.
- Tests are async (pytest-asyncio, `asyncio_mode = "auto"`, so no `@pytest.mark.asyncio`). Use
  the `client` / `db_client` fixtures: an `httpx2.AsyncClient` over ASGI with the app lifespan
  running. Write `(await client.post(...)).json()`, not `await client.post(...).json()`.
- Add dependencies with `uv add` / `uv add --dev` and commit `uv.lock`.

# Nevis Search API

FastAPI + raw asyncpg + Postgres 16, which also does the searching: `pg_trgm` for clients,
full-text search plus pgvector for documents, with local fastembed models baked into the image
(`decisions.md`). A separate `worker` service writes document summaries. Schema in
`schema/schema.sql`; Atlas generates and applies versioned migrations from it. Assignment spec:
`task.md`.

## Commands

Everyday commands are `just` recipes (`just` lists them; see `justfile`).

- Install: `uv sync`. Atlas runs only in Docker (the compose `migrate` / `migrate-diff`
  services); no local CLI.
- Whole system in Docker: `just up` → http://localhost:8000/docs. The api and the summary
  worker run the `dev` image stage with `src/` mounted; the api reloads on changes, the worker
  doesn't (`docker compose restart worker`). `just logs`, `just worker-logs`, `just down`.
- Infra only, api on the host (to debug it): `just infra`, then `just run` (or your debugger
  with `NEVIS_DATABASE_URL=postgresql://nevis:nevis@localhost:5432/nevis`). On the host the models
  download to fastembed's temp-dir cache on first use (`NEVIS_MODEL_CACHE_DIR` to keep them).
- Dev data lives in `.data/postgres` (bind mount), so it survives `just down`; `just clean`
  deletes it. `just reembed` embeds documents that have no chunks (`--all`: every document, after
  a model or chunking change) and refreshes clients' `search_text`.
- Tests in Docker (80% coverage gate): `just test [pytest args]`. Uses the separate test stack
  (`docker-compose.test.yml`: port 55432, tmpfs) and runs pytest in a `tests` container with the
  repository mounted.
- Tests on the host: `just test-infra` (re-run after adding a migration), then `uv run pytest`.
  If integration tests are selected and the test database isn't reachable, named `*_test` and
  migrated, pytest aborts before running anything. `uv run pytest -m unit` needs no
  infrastructure. The URL defaults to localhost:55432 (`NEVIS_TEST_DATABASE_URL`).
- Lint, format check, type check (strict): `just check`; fix with
  `uv run ruff check --fix . && uv run ruff format .`
- Don't use `docker compose up --wait migrate`: `--wait` reports a one-shot as failed even when
  it exits 0. Wait for `db`, then `docker compose run --rm migrate`.

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
- Postgres extensions (`vector`, `pg_trgm`) are an Atlas Pro feature. Create them outside Atlas,
  in `docker/initdb/02-extensions.sql` (used by both stacks), in both the app and `atlas_dev`
  databases. An existing dev database needs them once by hand (command in that file's header).
- Stored generated columns (`documents.tsv`) are left out of the row models: select explicit
  columns (`nevis.documents.columns.DOCUMENT_COLUMNS`), never `SELECT *` on documents.
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
- Configuration comes only from `nevis.config` (`NEVIS_*` env vars): `Settings` for the api,
  `WorkerSettings` for the summary worker. `NEVIS_DATABASE_URL` must be a plain `postgresql://`
  DSN; asyncpg rejects driver-qualified schemes.
- Database access is raw SQL through the asyncpg pool (`PoolDep`). Validate rows into the
  generated `nevis.db_models` classes; request/response models live in each feature's `schemas.py`.
- Inject the pool, not a connection: a connection dependency is acquired before FastAPI rejects
  an invalid body. Use `pool.fetchrow(...)` for single statements, `pool.acquire()` for transactions.
- Search data is written with the row it describes, in the same transaction: a client's
  `search_text` (`nevis.text.client_search_text`), a document's chunks and embeddings. Embed
  before opening the transaction (it takes seconds for long documents), never inside it.
- Search session settings (typo threshold, `hnsw.ef_search`) are set per pool connection
  (`nevis.db.SERVER_SETTINGS`), so search queries need no transaction.
- The summary worker finds work by `summary IS NULL` (no job table) and must stay safe to
  repeat: claims hold a lease, writes are guarded by `summary IS NULL`.
- Mark tests `unit` (no external services) or `integration` (needs Postgres). Test apps never
  run the summary worker in the background: drive it with `SummaryWorker.run_once()`.
- Tests are async (pytest-asyncio, `asyncio_mode = "auto"`, so no `@pytest.mark.asyncio`). Use
  the `client` / `db_client` fixtures: an `httpx2.AsyncClient` over ASGI with the app lifespan
  running. Write `(await client.post(...)).json()`, not `await client.post(...).json()`.
- Add dependencies with `uv add` / `uv add --dev` and commit `uv.lock`.

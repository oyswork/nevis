# Developer commands. `just` (or `just help`) lists them.
# Arguments reach recipes as "$@" / "$1", so values with spaces (pytest -k "a and b") survive.

set positional-arguments := true

# Same .env and defaults as docker-compose.yml, so `just run` targets the database compose starts.

set dotenv-load := true

# Recipes start the db with `up --wait` and then `run --rm` the one-shot (migrate): `--wait`
# reports a one-shot as failed even when it exits 0, while `run` blocks until it finishes and
# returns its exit code, so nothing starts against a half-set-up stack.

compose := "docker compose"
test_compose := "docker compose -f docker-compose.yml -f docker-compose.test.yml"
dev_database_url := "postgresql://" + env("POSTGRES_USER", "nevis") + ":" + env("POSTGRES_PASSWORD", "nevis") + "@localhost:" + env("POSTGRES_PORT", "5432") + "/" + env("POSTGRES_DB", "nevis")

# List the available recipes
[group("help")]
help:
    @just --list --unsorted

# Run the whole system in Docker (db, migrations, api with auto-reload, summary worker) on :8000
[group("run")]
up:
    {{ compose }} up --build --detach --wait
    @echo "API: http://localhost:8000/docs  (logs: just logs)"

# Run only the infrastructure (db, migrations) in Docker, no worker; then `just run`
[group("run")]
infra:
    {{ compose }} stop api
    {{ compose }} up --detach --wait db
    {{ compose }} run --rm migrate

# Run the api on the host with auto-reload, against the infrastructure from `just infra`
[group("run")]
run: infra
    NEVIS_DATABASE_URL={{ dev_database_url }} uv run uvicorn --factory nevis.main:create_app --reload

# Follow the logs of the dev stack (all services, or the given ones)
[group("run")]
logs *services:
    {{ compose }} logs --follow "$@"

# Stop the dev and test stacks (dev data in .data/ is kept)
[group("run")]
down:
    {{ compose }} down
    {{ test_compose }} down

# Run the test suite in Docker against a throwaway database; extra args go to pytest
[group("test")]
test *args:
    {{ test_compose }} run --rm --build tests pytest "$@"

# Start only the test database, to run or debug `uv run pytest` on the host
[group("test")]
test-infra:
    {{ test_compose }} up --detach --wait db
    {{ test_compose }} run --rm migrate

# Smoke-test the production stack (release images, Caddy, HTTPS, auth) on 127.0.0.1:80/443
[group("test")]
prod-smoke:
    deploy/smoke.sh

# Exercise the whole system on a throwaway stack with a local model (see scripts/exercise.sh)
[group("test")]
exercise *args:
    scripts/exercise.sh "$@"

# Measure search relevance on a throwaway stack: 58 labelled queries in 5 domains (relevance.py)
[group("test")]
relevance *args:
    scripts/relevance.sh "$@"

# Lint, format check and type check on the host
[group("test")]
check:
    uv run ruff check .
    uv run ruff format --check .
    uv run mypy

# Apply pending migrations to the dev database
[group("database")]
migrate:
    {{ compose }} run --rm migrate

# Embed documents that have no chunks yet (--all: every document) and refresh clients' search_text
[group("search")]
reembed *args:
    {{ compose }} run --rm api python -m nevis.search.reembed "$@"

# Generate a migration from schema/schema.sql changes and regenerate the models: `just migration add_x`
[group("database")]
migration name: && models _schema-commit-note
    {{ compose }} run --rm migrate-diff "$1"

# Regenerate src/nevis/db_models.py from schema/schema.sql
[group("database")]
models:
    uv run python scripts/generate_models.py

# A schema change is one commit: the source, its migration and the models generated from it.
[private]
_schema-commit-note:
    @echo
    @echo "Review the generated SQL, then commit these together (schema, migration, atlas.sum, models):"
    @git status --short -- schema src/nevis/db_models.py

# Stop the dev stack and delete the dev database in .data/
[confirm("Delete the dev database in .data/?")]
[group("database")]
clean:
    {{ compose }} down
    rm -rf .data/postgres

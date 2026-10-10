# Decisions

## Schema in SQL: Atlas-generated migrations, generated pydantic models, raw SQL queries

**Decision.** `schema/schema.sql` is the single source of truth for the database:

- **Atlas** generates versioned migrations from it: `atlas migrate diff` compares the migration
  history in `schema/migrations/` with the file and writes the difference as a new SQL
  migration. Nobody writes migrations by hand, but each one is reviewed and committed, and
  `atlas migrate apply` runs the pending ones and records them in `atlas_schema_revisions`.
- **omymodels** generates the pydantic row models in `src/nevis/db_models.py` from the same
  file (`scripts/generate_models.py`). A test fails if the generated file is stale.
- Queries are **raw SQL** through an asyncpg pool; rows are validated into the generated models.

**Why.**

- **One source of truth.** The table definitions live in exactly one place, written in the
  language the database speaks. Migrations and Python models are both generated from it, not
  kept in step with it by hand.
- **History without hand-written migrations.** Generated migration files keep a reviewable
  record of every schema change and of exactly what SQL ran against each database. Applying
  `schema.sql` declaratively, which this project did at first, left no such record: a
  destructive change, such as dropping a column, was visible only in Atlas's planned diff.
- **No heavy dependencies.** The usual alternative, SQLAlchemy models plus Alembic migrations,
  pulls in a large ORM and a migration framework just to describe tables and evolve them. Here
  the runtime needs only asyncpg and pydantic; Atlas runs in Docker and omymodels is a dev-only
  dependency.
- **Raw SQL over an ORM.** The author normally prefers raw SQL to ORMs:
  - You can tell at a glance which queries run: the SQL in the repository is what reaches
    Postgres.
  - You get fine control over each statement, including CTEs, `RETURNING`, index-friendly
    predicates and Postgres-specific features such as pgvector.
  - Optimizing means reading the query, not investigating what SQL the ORM generates and then
    working out how to coax it into something better.

**Trade-offs.**

- No ORM conveniences such as relationships, lazy loading or a query builder. Mapping rows to
  models and composing queries is done by hand.
- Postgres extensions (e.g. `vector`) are an Atlas Pro feature, so they are created outside
  Atlas, in `docker/initdb/`.
- The model generator's type mapping has quirks. For example, `timestamptz` must be spelled that
  way to become a timezone-aware datetime (see `CLAUDE.md`).
- Forgetting to run `migrate diff` after editing `schema.sql` leaves the migrations behind the
  file, and nothing checks for it automatically yet. Applied migrations are immutable: fixes go
  in a new migration, not an edit.

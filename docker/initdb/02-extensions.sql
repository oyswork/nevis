-- Extensions search needs, created here because they are an Atlas Pro feature: in the app
-- database (POSTGRES_DB, the default database of these scripts) and in the atlas_dev scratch
-- database that `migrate diff` uses (01-atlas-dev.sql creates it).
-- Scripts here only run on an empty data directory. For an existing database, run once:
--   for db in nevis atlas_dev; do docker compose exec db psql -U nevis -d $db \
--     -c 'CREATE EXTENSION IF NOT EXISTS vector' -c 'CREATE EXTENSION IF NOT EXISTS pg_trgm'; done
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

\c atlas_dev
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

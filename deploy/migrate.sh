#!/usr/bin/env bash
# Prepares a production database for a release, from wherever this runs (CI, through an SSH
# tunnel to the server): creates the extensions search needs (an Atlas Pro feature, so not in the
# migrations), then applies schema/migrations with Atlas. Both run in containers; nothing to
# install. The server never runs Atlas.
#
# Connection: the standard PGHOST, PGPORT, PGUSER, PGPASSWORD, PGDATABASE variables. MIGRATE_NETWORK
# is the Docker network the containers join (default: host, for a tunnel on this machine).
set -euo pipefail

cd "$(dirname "$0")/.."

: "${PGHOST:?}" "${PGUSER:?}" "${PGPASSWORD:?}" "${PGDATABASE:?}"
PGPORT=${PGPORT:-5432}
NETWORK=${MIGRATE_NETWORK:-host}
POSTGRES_IMAGE=pgvector/pgvector:pg16
ATLAS_IMAGE=arigaio/atlas:1.3.3

echo "== extensions"
docker run --rm --network "$NETWORK" \
	-e PGHOST -e PGPORT="$PGPORT" -e PGUSER -e PGPASSWORD -e PGDATABASE \
	"$POSTGRES_IMAGE" psql --quiet -v ON_ERROR_STOP=1 \
	-c 'CREATE EXTENSION IF NOT EXISTS vector' \
	-c 'CREATE EXTENSION IF NOT EXISTS pg_trgm'

# Atlas wants a URL: quote the credentials, which may hold URL syntax.
url=$(PGPORT=$PGPORT python3 -c '
import os
from urllib.parse import quote
user, password, host, port, database = (
    os.environ[name] for name in ("PGUSER", "PGPASSWORD", "PGHOST", "PGPORT", "PGDATABASE")
)
print("postgresql://%s:%s@%s:%s/%s?sslmode=disable&search_path=public" % (
    quote(user, safe=""), quote(password, safe=""), host, port, quote(database, safe="")))
')

echo "== migrations"
# Each migration file runs in a transaction: a failing one leaves the database as it was.
docker run --rm --network "$NETWORK" -v "$PWD/schema:/schema:ro" -w /schema \
	-e ATLAS_URL="$url" "$ATLAS_IMAGE" migrate apply --env compose

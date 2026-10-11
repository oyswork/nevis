#!/usr/bin/env bash
# Smoke test of the production stack (deploy/compose.prod.yml): builds the two release images
# locally and deploys them in the workflow's order (Postgres, then deploy/migrate.sh, then the
# rest) on 127.0.0.1:80/443, with Caddy's internal CA instead of Let's Encrypt. Then checks HTTPS,
# auth, rate limits and a write followed by a search. Runs in CI on every push
# (`prod-smoke`) and locally with `just prod-smoke`. Leaves no containers or volumes behind.
set -euo pipefail

cd "$(dirname "$0")/.."

PROJECT=nevis-smoke
PREFIX=nevis-smoke.local
TAG=smoke
HOST=127.0.0.1
USER_NAME=smoke
PASSWORD=smoke-password
READY_ATTEMPTS=60
DB_PASSWORD=smoke-password
# Off the dev (5432) and test (55432) databases' ports.
DB_PORT=55433

work=$(mktemp -d)
compose=(docker compose --project-name "$PROJECT" --env-file "$work/.env" -f deploy/compose.prod.yml)

cleanup() {
	local status=$?
	if ((status != 0)); then
		"${compose[@]}" logs --no-color --tail 50 caddy api || true
	fi
	"${compose[@]}" down --volumes --remove-orphans > /dev/null 2>&1 || true
	rm -rf "$work"
	exit "$status"
}
trap cleanup EXIT

fail() {
	echo "FAIL: $*" >&2
	exit 1
}

echo "== building images"
docker build --quiet --target runtime --tag "$PREFIX/nevis:$TAG" . > /dev/null
docker build --quiet --target proxy --tag "$PREFIX/nevis-proxy:$TAG" . > /dev/null

hash=$(docker run --rm "$PREFIX/nevis-proxy:$TAG" caddy hash-password --plaintext "$PASSWORD")
# Single-quoted like the server's .env must be: the hash is full of `$`.
cat > "$work/.env" << EOF
NEVIS_PUBLIC_IP=$HOST
NEVIS_TLS=internal
NEVIS_IMAGE_PREFIX=$PREFIX
NEVIS_IMAGE_TAG=$TAG
POSTGRES_PASSWORD=$DB_PASSWORD
NEVIS_DB_PORT=$DB_PORT
NEVIS_BASIC_AUTH_USER=$USER_NAME
NEVIS_BASIC_AUTH_HASH='$hash'
EOF

echo "== starting the stack"
"${compose[@]}" up --detach --wait --quiet-pull db
# The workflow migrates through a tunnel on the host network; here the containers join the
# stack's network instead.
MIGRATE_NETWORK=${PROJECT}_default PGHOST=db PGUSER=nevis PGPASSWORD=$DB_PASSWORD PGDATABASE=nevis \
	deploy/migrate.sh
"${compose[@]}" up --detach --wait --quiet-pull

# Trust Caddy's local root, so certificates are verified rather than skipped. No SNI is sent to an
# IP address, so this also proves `default_sni` selects the certificate.
for _ in $(seq "$READY_ATTEMPTS"); do
	"${compose[@]}" cp caddy:/data/caddy/pki/authorities/local/root.crt "$work/root.crt" 2> /dev/null \
		&& curl --silent --fail --cacert "$work/root.crt" "https://$HOST/health" > /dev/null \
		&& break
	sleep 1
done
[[ -s $work/root.crt ]] || fail "Caddy never issued its internal certificate"

https=(curl --silent --show-error --cacert "$work/root.crt" --max-time 30)
auth=(--user "$USER_NAME:$PASSWORD")

status() {
	"${https[@]}" --output /dev/null --write-out '%{http_code}' "$@"
}

expect() {
	local want=$1 got
	shift
	got=$(status "$@")
	[[ $got == "$want" ]] || fail "expected $want, got $got: $*"
	echo "ok  $want  ${*: -1}"
}

echo "== checks"
expect 200 "https://$HOST/health/ready"
expect 401 "https://$HOST/search?q=anything"
expect 401 --user "$USER_NAME:wrong" "https://$HOST/search?q=anything"
expect 200 "${auth[@]}" "https://$HOST/openapi.json"

email="smoke.$RANDOM@smoketest.example"
expect 201 "${auth[@]}" -H 'Content-Type: application/json' \
	-d "{\"first_name\": \"Smoke\", \"last_name\": \"Test\", \"email\": \"$email\"}" \
	"https://$HOST/clients"
"${https[@]}" "${auth[@]}" "https://$HOST/search?q=smoketest&documents_limit=0" | grep -q "$email" \
	|| fail "the new client isn't found by search"
echo "ok  search finds the new client"

# The re-embedding tool (`just reembed` in development) runs from the release image too.
"${compose[@]}" run --rm --no-deps api python -m nevis.search.reembed > "$work/reembed.log" 2>&1 \
	|| { cat "$work/reembed.log"; fail "reembed failed"; }
echo "ok  reembed runs from the release image"

redirect=$(curl --silent --output /dev/null --write-out '%{http_code} %{redirect_url}' "http://$HOST/health")
[[ $redirect == "308 https://$HOST/health" ]] || fail "expected a 308 to https, got: $redirect"
echo "ok  308  http -> https"

# Last: it uses up the window for this client.
limited=0
for _ in $(seq 40); do
	[[ $(status "https://$HOST/health") == 429 ]] && limited=1 && break
done
((limited)) || fail "40 requests in a row were never rate limited"
echo "ok  429  rate limit"

echo "== smoke test passed"

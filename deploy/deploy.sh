#!/usr/bin/env bash
# Server side of a deploy, driven step by step by .github/workflows/deploy.yml over SSH. The only
# command the CI deploy key may run (authorized_keys: command="/opt/nevis/deploy.sh",...), so the
# arguments arrive in SSH_ORIGINAL_COMMAND. Lives in /opt/nevis next to .env (deploy/bootstrap.md).
#
#   deploy.sh prepare <version> < compose.prod.yml   stage the release's compose file, pull its
#                                                    images, make sure Postgres runs
#   deploy.sh backup <version>                       pg_dump into backups/, keeping the last 7
#   deploy.sh start <version>                        switch to the release, record it, check HTTPS
#
# Between backup and start the workflow applies the migrations from CI (deploy/migrate.sh). No
# automatic rollback: deploy the previous version, and restore the backup if a migration ran.
set -euo pipefail
# Backups hold client data, and .env the secrets: nothing created here is readable by others.
umask 077

NEVIS_DIR=${NEVIS_DIR:-/opt/nevis}
BACKUPS_KEPT=7
READY_ATTEMPTS=60
# Image tags allow no `+`, so no build metadata (the release job checks the same).
VERSION_PATTERN='^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$'
USAGE="usage: deploy.sh prepare|backup|start <version>"

fail() {
	echo "deploy: $*" >&2
	exit 1
}

read -r action version extra <<< "${SSH_ORIGINAL_COMMAND:-$*}" || true
[[ $version =~ $VERSION_PATTERN && -z ${extra:-} ]] || fail "$USAGE (got '${SSH_ORIGINAL_COMMAND:-$*}')"

cd "$NEVIS_DIR"
[[ -f .env ]] || fail "$NEVIS_DIR/.env is missing (deploy/bootstrap.md)"
# The shell environment overrides .env, so the release's tag is used before start records it.
export NEVIS_IMAGE_TAG=$version

prepare() {
	# Global: the EXIT trap runs after this function has returned.
	staged=$(mktemp compose.yaml.XXXXXX)
	trap 'rm -f "$staged"' EXIT
	cat > "$staged"
	[[ -s $staged ]] || fail "no compose file on stdin"
	docker compose -f "$staged" config --quiet || fail "the compose file doesn't validate against .env"
	chmod 644 "$staged"
	mv "$staged" "compose.next.yaml"
	echo "== pulling $version"
	docker compose -f compose.next.yaml pull --quiet
	docker compose -f compose.next.yaml up --detach --wait db
}

backup() {
	local file backups
	file="backups/nevis-$(date -u +%Y%m%dT%H%M%SZ)-before-$version.dump"
	mkdir -p backups
	echo "== backing up the database to $file"
	# Any compose file works here: the containers belong to the project, whichever file names them.
	docker compose -f compose.next.yaml exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -Fc "$POSTGRES_DB"' \
		> "$file.partial"
	mv "$file.partial" "$file"
	# The names start with a UTC timestamp, so the glob lists them oldest first.
	backups=(backups/*.dump)
	if ((${#backups[@]} > BACKUPS_KEPT)); then
		rm -- "${backups[@]:0:${#backups[@]}-BACKUPS_KEPT}"
	fi
}

start() {
	local env_file public_ip
	[[ -f compose.next.yaml ]] || fail "nothing prepared: run prepare first"
	mv compose.next.yaml compose.yaml
	echo "== starting $version"
	docker compose up --detach --wait --remove-orphans --quiet-pull

	# Record what runs, so plain `docker compose ps|logs|restart` in this directory keep working.
	env_file=$(mktemp .env.XXXXXX)
	grep -v '^NEVIS_IMAGE_TAG=' .env > "$env_file" || true
	echo "NEVIS_IMAGE_TAG=$version" >> "$env_file"
	mv "$env_file" .env
	echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $version" >> DEPLOYED

	# Through Caddy, verifying the certificate for the public IP, but connecting locally: the
	# public address may not loop back from the machine itself (e.g. behind a cloud provider's
	# NAT). The first deploy waits for Let's Encrypt.
	public_ip=$(sed -n "s/^NEVIS_PUBLIC_IP=['\"]\{0,1\}\([^'\"]*\).*/\1/p" .env)
	echo "== checking https://$public_ip/health/ready"
	for _ in $(seq "$READY_ATTEMPTS"); do
		if curl --silent --fail --max-time 10 --connect-to "$public_ip:443:127.0.0.1:443" \
			"https://$public_ip/health/ready" > /dev/null; then
			echo "== $version is live"
			return
		fi
		sleep 5
	done
	fail "$version started but https://$public_ip/health/ready isn't answering: docker compose logs caddy api"
}

case $action in
	prepare) prepare ;;
	backup) [[ -f compose.next.yaml ]] || fail "nothing prepared: run prepare first"; backup ;;
	start) start ;;
	*) fail "$USAGE" ;;
esac

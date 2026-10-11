#!/usr/bin/env bash
# Measure search relevance on a throwaway stack (docker-compose.exercise.yml: database in memory,
# api on 127.0.0.1:58000): loads scripts/relevance_corpus.json and prints hit@1, hit@3 and MRR per
# domain (scripts/relevance.py), then removes the stack. The dev stack and its data are untouched.
#
#   just relevance [relevance.py options]
set -euo pipefail

cd "$(dirname "$0")/.."

API_URL=http://127.0.0.1:58000
compose=(docker compose -f docker-compose.yml -f docker-compose.exercise.yml)

cleanup() {
	local status=$?
	if ((status != 0)); then
		"${compose[@]}" logs --no-color --tail 40 api || true
	fi
	"${compose[@]}" down --volumes --remove-orphans > /dev/null 2>&1 || true
	exit "$status"
}
trap cleanup EXIT

echo "== starting the stack"
"${compose[@]}" up --build --detach --wait --quiet-pull

uv run python scripts/relevance.py --base-url "$API_URL" "$@"

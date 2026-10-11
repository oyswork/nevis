#!/usr/bin/env bash
# Exercise the whole system end to end, summaries included: starts a throwaway copy of the dev
# stack (docker-compose.exercise.yml: database in memory, api on 127.0.0.1:58000) whose summary
# worker uses a locally running model, runs scripts/exercise.py against it and removes the stack.
# The dev stack and its data are untouched.
#
#   just exercise [--summary-url URL] [--model ID] [--keep] [-- exercise.py options]
#
# --summary-url is the model's OpenAI-compatible API as seen from the host (default LM Studio's
# http://127.0.0.1:1234/v1); the worker reaches it through host.docker.internal. Without
# --model, the first model the server lists that isn't an embedding model is used. --keep leaves
# the stack running afterwards (remove it with `docker compose -p nevis-exercise down -v`).
set -euo pipefail

cd "$(dirname "$0")/.."

API_URL=http://127.0.0.1:58000
compose=(docker compose -f docker-compose.yml -f docker-compose.exercise.yml)

summary_url=http://127.0.0.1:1234/v1
model=
keep=0
while (($#)); do
	case $1 in
		--summary-url) summary_url=${2:?--summary-url needs a URL} && shift 2 ;;
		--model) model=${2:?--model needs a model id} && shift 2 ;;
		--keep) keep=1 && shift ;;
		--) shift && break ;;
		*) echo "unknown option: $1" >&2 && exit 2 ;;
	esac
done
summary_url=${summary_url%/}

fail() {
	echo "FAIL: $*" >&2
	exit 1
}

models=$(curl --silent --fail --max-time 5 "$summary_url/models") \
	|| fail "no model server at $summary_url (is it running?)"
if [[ -z $model ]]; then
	model=$(jq -r '[.data[].id | select(test("embed"; "i") | not)][0] // empty' <<< "$models")
	[[ -n $model ]] || fail "$summary_url lists no chat model: $models"
fi
jq -e --arg model "$model" 'any(.data[]; .id == $model)' <<< "$models" > /dev/null \
	|| fail "$summary_url doesn't serve $model"
# The host's loopback is host.docker.internal from inside a container.
container_url=$(sed -E 's#//(127\.0\.0\.1|localhost)([:/]|$)#//host.docker.internal\2#' <<< "$summary_url")
echo "== summary model: $model at $container_url (from the worker)"

cleanup() {
	local status=$?
	if ((status != 0)); then
		"${compose[@]}" logs --no-color --tail 40 api worker || true
	fi
	if ((keep)); then
		echo "== the stack is still running: $API_URL/docs"
	else
		"${compose[@]}" down --volumes --remove-orphans > /dev/null 2>&1 || true
	fi
	exit "$status"
}
trap cleanup EXIT

echo "== starting the stack"
NEVIS_SUMMARY_API_URL=$container_url NEVIS_SUMMARY_MODEL=$model \
	"${compose[@]}" up --build --detach --wait --quiet-pull

uv run python scripts/exercise.py --base-url "$API_URL" "$@"

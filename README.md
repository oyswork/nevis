# Nevis Search API

Search across advisor clients and their documents: clients by name, email or description
(including part of a word), documents by meaning as well as words. Spec: [`task.md`](task.md).

FastAPI, Postgres 16 (source of truth) and Meilisearch (search index, with a built-in local
embedding model). Why these, and the trade-offs: [`decisions.md`](decisions.md).

## Quick start

Needs Docker. [`just`](https://just.systems) runs the everyday commands; plain `docker compose`
works too.

```sh
just up                          # or: docker compose up --build --detach --wait
open http://localhost:8000/docs  # Swagger UI; OpenAPI at /openapi.json
```

The first start downloads the embedding model (about 30 s); it's cached afterwards.
`just down` stops everything; data stays in `.data/` until `just clean`.

## API

| Method and path | Does | Errors |
|---|---|---|
| `POST /clients` | Create a client | 409 email taken (case-insensitive), 422 |
| `GET /clients/{id}` | Read a client | 404, 422 |
| `POST /clients/{id}/documents` | Add a document to a client | 404 unknown client, 422 |
| `GET /documents/{id}` | Read a document (with its summary, once there is one) | 404, 422 |
| `GET /search?q=` | Search clients, then documents | 422 |
| `GET /health`, `GET /health/ready` | Liveness; readiness (Postgres and Meilisearch reachable) | |

Endpoints return 503 when Postgres, or for search Meilisearch, is unreachable. Full schemas,
limits and examples are in the Swagger UI.

**Search is eventually consistent:** a new client or document becomes searchable within about a
second, once the background worker has indexed it.

## Examples

Responses below are real, from a fresh stack; long fields are trimmed with `…`.

### Create a client and a document

```sh
curl -s -X POST localhost:8000/clients -H 'Content-Type: application/json' -d '{
  "first_name": "John", "last_name": "Doe", "email": "john.doe@neviswealth.com",
  "description": "Prefers sustainable funds; retiring in 2030",
  "social_links": ["https://www.linkedin.com/in/johndoe"]
}'
```

```json
{
  "id": "676eb0f9-2e79-4305-8e54-2d0b94f5cdfb",
  "first_name": "John",
  "last_name": "Doe",
  "email": "john.doe@neviswealth.com",
  "description": "Prefers sustainable funds; retiring in 2030",
  "social_links": ["https://www.linkedin.com/in/johndoe"]
}
```

```sh
curl -s -X POST localhost:8000/clients/676eb0f9-2e79-4305-8e54-2d0b94f5cdfb/documents \
  -H 'Content-Type: application/json' -d '{
  "title": "Utility bill March",
  "content": "Electricity utility bill from Vattenfall for the apartment at 12 Lake Road, Zurich. Billing period March 2026, amount due CHF 84.20."
}'
```

```json
{
  "id": "0bf6e465-e718-40fb-b65e-fbe5b081743c",
  "client_id": "676eb0f9-2e79-4305-8e54-2d0b94f5cdfb",
  "title": "Utility bill March",
  "content": "Electricity utility bill from Vattenfall for the apartment at 12 Lake Road, …",
  "summary": null,
  "created_at": "2026-10-10T14:19:42.049995Z"
}
```

The examples below also use two more clients (Zoë McAllister, and Anna Schmidt whose
description says "Referred by NevisWealth") and two more documents of John's ("Passport scan",
"Tax return 2025").

### Find a client by email: `NevisWealth`

```sh
curl -s 'localhost:8000/search?q=NevisWealth&documents_limit=2'
```

```json
[
  {"type": "client", "client": {"id": "676eb0f9-…", "first_name": "John", "last_name": "Doe",
    "email": "john.doe@neviswealth.com", "description": "Prefers sustainable funds; …", "social_links": ["…"]}},
  {"type": "client", "client": {"id": "f38811a9-…", "first_name": "Anna", "last_name": "Schmidt",
    "email": "anna@weberfamilyoffice.de", "description": "Referred by NevisWealth", "social_links": []}},
  {"type": "document", "document": {"id": "0bf6e465-…", "title": "Utility bill March", "…": "…"},
    "matched_text": "Electricity utility bill from Vattenfall for the apartment at 12 Lake Road, …"},
  {"type": "document", "document": {"id": "55892618-…", "title": "Passport scan", "…": "…"},
    "matched_text": "Scanned copy of the client's Swiss passport, issued 2021, valid until 2031."}
]
```

Clients come first: keyword matches, then ones containing the text inside a word. `wealth`
also finds John (`neviswealth`) and Anna, regardless of case and accents.

### Find documents by meaning: `address proof`

```sh
curl -s 'localhost:8000/search?q=address%20proof&documents_limit=2'
```

```json
[
  {"type": "document", "document": {"id": "55892618-…", "title": "Passport scan", "…": "…"},
    "matched_text": "Scanned copy of the client's Swiss passport, issued 2021, valid until 2031."},
  {"type": "document", "document": {"id": "0bf6e465-…", "title": "Utility bill March", "…": "…"},
    "matched_text": "Electricity utility bill from Vattenfall for the apartment at 12 Lake Road, …"}
]
```

The utility bill is found although it contains neither word. It comes second: the local
embedding model rates identity documents as closer to "proof"
([`decisions.md`](decisions.md) has the measurements).

### Errors

```sh
curl -s -X POST localhost:8000/clients -H 'Content-Type: application/json' \
  -d '{"first_name": "John", "last_name": "Doe", "email": "John.Doe@NevisWealth.com"}'
# 409 {"detail": "A client with this email already exists"}
```

## How search works

- **Clients:** Meilisearch keyword search (typo-tolerant, prefix on the last word), plus the
  experimental `CONTAINS` filter for text inside a word. Keyword matches rank first.
- **Documents:** content is split into overlapping chunks; each is embedded by Meilisearch's
  built-in `BAAI/bge-small-en-v1.5` and searched by keyword and meaning together. One result per
  document, with the chunk that matched.
- **Limits, not cutoffs:** semantic search always has a "closest" document, so `documents_limit`
  (default 5) bounds the list; there is no relevance score in responses (see `decisions.md`).
- **Consistency:** writes go to Postgres together with an outbox job; a background worker
  indexes them, retrying until Meilisearch accepts them. `just reindex` rebuilds the index.

## Summaries (optional, local model only)

Documents get a `summary` only if a locally running model with an OpenAI-compatible API is
configured; otherwise it stays `null`. With [Ollama](https://ollama.com) on the host:

```sh
ollama pull llama3.2
cat >> .env <<'ENV'
NEVIS_SUMMARY_API_URL=http://host.docker.internal:11434/v1
NEVIS_SUMMARY_MODEL=llama3.2
ENV
just up
```

On Linux, start Ollama with `OLLAMA_HOST=0.0.0.0` so the api container can reach it. The summary
appears shortly after the document is created: `GET /documents/{id}`.

## Development

| Command | Does |
|---|---|
| `just test [pytest args]` | Full test suite in Docker (separate, in-memory stack; 80% coverage gate) |
| `just check` | Ruff lint and format check, mypy (strict) |
| `just infra`, then `just run` | Infrastructure in Docker, api on the host (to debug it) |
| `just migration <name>` | Generate a migration from `schema/schema.sql` (and the row models) |
| `just search-setup`, `just reindex` | Apply index settings; rebuild the index from Postgres |
| `just logs`, `just down`, `just clean` | Follow logs; stop; delete dev data |

Needs [uv](https://docs.astral.sh/uv/) for the host commands (`uv sync`). Configuration is
read from `NEVIS_*` environment variables; `.env.example` lists the compose overrides.

Not deployed anywhere.

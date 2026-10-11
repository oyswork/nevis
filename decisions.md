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

## Search: Postgres only, with `pg_trgm` and `pgvector`

**Decision.**

- Clients: name, email and description are normalized in Python (NFKD, accent marks dropped,
  casefolded, letters such as `ø`, `ł`, `æ` mapped) into `clients.search_text`, with a trigram
  GIN index. Whole-word matches rank first, then matches inside a word (`LIKE`), then typos
  (`word_similarity` of at least 0.5, via the index-assisted `<%`).
- Documents: chunks with `bge-small-en-v1.5` embeddings in `document_chunks` (HNSW, cosine), and
  a generated `tsvector` over title and content. The nearest 200 chunks (best chunk per document,
  top 50) and the top 50 full-text matches on any query word are merged by reciprocal rank
  fusion (k = 60). The top 20 go on, cut to `documents_limit`.
- Writes: `POST` embeds a document's chunks before its transaction, then commits the document
  and its chunks together. Search is consistent immediately: no outbox, worker or reindex.
- The models run in the api with fastembed (ONNX on the CPU) and are baked into the image.
  `just reembed` embeds documents that predate their chunks.
- A MiniLM cross-encoder can rerank the top 20 (`NEVIS_SEARCH_RERANK`); off by default.

**Why.**

- One datastore: no second copy of the data to keep in step, and writes are visible at once.
- Same quality as Meilisearch in a spike: on 13 labelled queries over 8 documents, both got 12/13
  top-1 and 13/13 recall@5. `pg_trgm` matched every substring, case and accent case, and typos
  (`neviswelth` → `neviswealth` 0.64, noise 0.25 or below).
- Built-in full-text search is enough: BM25 (ParadeDB, AGPL) gave the same results.
- Normalization in Python, not `unaccent`: indexing `unaccent()` needs an IMMUTABLE wrapper
  function, and functions are an Atlas Pro feature.
- Reranker off: on 15 documents it matched plain vector order (18/20 top-1) at 80–220 ms per
  search, and the first spike's "utility bill first for address proof" didn't reproduce.

**Trade-offs.**

- Relevance is built by hand (tiers, fusion, query sanitizing) rather than configured.
- Embedding runs in the request, on a two-thread executor shared by all requests: about 1.3 s for
  a 100k-character document.
- The models need ~380 MB of memory in the api (batches of 4; the defaults peaked at 1.2 GB) and
  add ~150 MB to the image.
- No relevance cutoff: neither cross-encoder scores nor cosine similarity separate junk from
  relevant results without losing recall. Documents are capped, so a junk query still returns
  the nearest ones.
- Queries are plain words: operators are stripped, so quoted phrases and `-word` become words
  matched with OR.
- Full-text search sees raw text, so it's accent-sensitive; vector search covers documents that
  differ only in accents.
- Embeddings don't know domain links: "address proof" ranks the utility bill 5th–8th among 15
  documents (2nd among the README's 3).
- Documents created before this change aren't found until `just reembed`.
- Queries under three characters, and very common substrings, scan clients rather than use the
  trigram index; fine at thousands of clients.

## Search: Meilisearch, with Postgres as the source of truth

*Superseded* by the section above. A spike showed Postgres matching Meilisearch's results
without a second datastore, the outbox, the reindex path or the experimental `CONTAINS` filter.
Kept for the record.

**Decision.**

- Meilisearch indexes clients and document chunks. The index is derived from Postgres and can
  be rebuilt from it (`just reindex`).
- Writes reach the index through a transactional outbox: the row and its outbox entry commit
  together, and a background worker indexes them with retries. Search is eventually consistent.
- Documents: hybrid search with Meilisearch's built-in Hugging Face embedder.
- Clients: keyword search merged, in one federated multi-search, with the experimental
  `CONTAINS` filter (`containsFilter`) for substring matches.

**Why.**

- Smaller, faster container than Elasticsearch (9.5.5 idles at ~1.4 GB with a 1 GB heap).
- Easier setup: a single binary, settings over HTTP, no mappings or analyzers to tune.
- Built-in embedder: no model or embedding code in the app. Elasticsearch's free license
  refuses inference and the RRF/linear retrievers, so both would be app-side.
- Morbid curiosity: the author wanted to try Meilisearch, and a test task is a good place to.

**Trade-offs.**

- No infix matching in regular search: whole words, a prefix on the last word, or typos.
  `wealth` doesn't find `neviswealth`. `CONTAINS` covers it, but:
  - it's experimental and may change between versions, so the image is pinned;
  - it scans: ~13 ms across 4 fields at 100k clients;
  - its hits score a flat 1.0, so they are weighted below keyword hits.
- A second datastore: an outbox, a worker and a reindex path to maintain. New records are
  briefly unsearchable.
- Semantic scores never drop below ~0.73, even for nonsense, so no score threshold works:
  - clients and documents are ranked separately, clients first (merging them let document noise
    push out real client matches);
  - documents are capped (`documents_limit`), not cut off: there is always a "closest" one;
  - responses carry no score (hybrid order doesn't even follow `_rankingScore`).
- Local embedders don't know domain links: "address proof" returns the utility bill, but not
  first. Larger models (up to bge-large, e5-large) did no better; whole open synonym lists
  (WordNet) added nothing over embeddings and bring sense noise (`bill` → `beak`).
- Text without a word (`--`) is a placeholder search in Meilisearch (matches everything), so
  keyword client search is skipped for it.
- The first index setup downloads the embedding model (cached in a volume afterwards).
- Rejected:
  - Elasticsearch: stable `wildcard` substring matching, but see above.
  - `pg_trgm` + `pgvector`: one datastore, but relevance features built by hand.

## Summaries: local model only, optional

**Decision.**

- `summary` is filled in by the summary worker (below) calling a locally running model over
  the OpenAI-compatible chat API (Ollama, LM Studio, llama.cpp, vLLM), set by
  `NEVIS_SUMMARY_API_URL` + `NEVIS_SUMMARY_MODEL`. Until the worker existed, an outbox job in
  the api did this (superseded with the outbox).
- Unset: the worker idles, `summary` stays null.
- No API key setting, so hosted LLM APIs aren't an option by accident.

**Trade-offs.**

- Asynchronous: `POST` returns `summary: null`; read it later with `GET /documents/{id}`.
- Input is cut to 12k characters to fit small local context windows.
- A summary is written once, never refreshed.
- Not verified against a real model in tests: the protocol is tested with a mock transport.

## Summaries: a separate worker service

**Decision.**

- The `worker` compose service (same image, `python -m nevis.summaries.worker`) writes
  summaries; the api never calls the model.
- It claims one document with `summary IS NULL` at a time under a 2-minute lease
  (`FOR UPDATE SKIP LOCKED`), gives the model 100 s, and writes the summary only if there is
  none yet.
- On failure it pushes the lease out by a backoff from 30 s, doubling up to 1 h, and counts
  `summary_attempts`. It never gives up.
- `POST /documents` sends `NOTIFY` in its transaction. The worker `LISTEN`s on its own connection
  (reconnecting when it drops) and sweeps every 60 s for missed notifications and expired leases.

**Why.**

- Model calls take seconds and fail independently of requests; a separate process keeps them off
  the api and its pool.
- Lease plus write-once makes runs safe to repeat: two workers never claim the same document, and
  a late or duplicate call can't overwrite a summary. A failure is recorded only while the
  worker still holds its lease.
- No job table: documents with `summary IS NULL` are the queue.

**Trade-offs.**

- A silently dropped `LISTEN` connection isn't noticed; the sweep bounds the delay to 60 s.
- A document that always fails is retried hourly forever; `summary_attempts` shows it.
- On shutdown an in-flight call is abandoned, and its document waits out the lease.
- The worker doesn't auto-reload in development: `docker compose restart worker`.

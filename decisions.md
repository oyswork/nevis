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
- The history was squashed into one migration before the first deploy, dropping the outbox
  and summary-job steps that were added and removed during development. Databases migrated
  before that had to be recreated; from the first deploy on, history is only appended.

## Search: Postgres only, with `pg_trgm` and `pgvector`

**Decision.**

- Clients: name, email and description are normalized in Python (NFKD, accent marks dropped,
  casefolded, letters such as `ø`, `ł`, `æ` mapped) into `clients.search_text`, with a trigram
  GIN index. Whole-word matches rank first, then matches inside a word (`LIKE`), then typos
  (`word_similarity` of at least 0.5, via the index-assisted `<%`).
- Documents: chunks with `bge-small-en-v1.5` embeddings in `document_chunks` (HNSW, cosine), and
  a generated `tsvector` over title and content. The nearest 200 chunks (best chunk per document,
  top 50) and the top 50 full-text matches on any query word, ranked by `ts_rank_cd` divided by
  document length, are merged by reciprocal rank fusion (k = 60). The top 20 go on, cut to
  `documents_limit`. Hits carry the document without its content (up to 100k characters each)
  and the chunk that matched.
- `embedding_model` records which model produced the stored vectors. The api refuses to start
  when it embeds with another one, and only `just reembed --all` updates the record.
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
- Length-normalized text rank: without it, long documents that repeat a common query word took
  over the text side as data grew. On the relevance corpus (`just relevance`: 58 queries, 48
  documents in five domains) plus 1,048 bulk documents, MRR went from 0.60 to 0.62 overall and
  from 0.43 to 0.53 on finance queries.
- The model check: vectors from two models can't be compared, and swapping one 384-dimension
  model for another fails silently otherwise.

**Trade-offs.**

- Relevance is built by hand (tiers, fusion, query sanitizing) rather than configured.
- Embedding runs in the request: 2–3.6 s for a 100k-character document (measured on an M4 Pro,
  2026-10-11). Documents embed two at a time and further uploads queue. Searches have a thread of
  their own, so they don't wait behind documents: during two concurrent 100k-character uploads the
  slowest search went from 9.8 s to 24 ms, with the same upload time and memory (Docker on an M4
  Pro, 2026-10-12). With the reranker on, concurrent searches take turns on that one thread.
- The models need ~380 MB of memory in the api (batches of 4; the defaults peaked at 1.2 GB) and
  add ~150 MB to the image.
- No relevance cutoff: neither cross-encoder scores nor cosine similarity separate junk from
  relevant results without losing recall. Documents are capped, so a junk query still returns
  the nearest ones.
- Queries are plain words: operators are stripped, so quoted phrases and `-word` become words
  matched with OR.
- Full-text search sees raw text, so it's accent-sensitive; vector search covers documents that
  differ only in accents.
- Embeddings don't know domain links, so where "address proof" ranks the utility bill depends on
  what else is stored: 2nd among the README's 3 documents, 1st in the relevance corpus, but
  outside the top 20 among 1,102 documents (measured before length normalization).
- English only. `just relevance` (2026-10-11): MRR 0.68 finance, 0.85 medical, 0.75 legal and
  0.73 software support, but 0.42 for German and cross-language queries (0.26 where they share
  no word with the document). Both models and the `english` text configuration are English-only.
  A 384-dimension multilingual model (`paraphrase-multilingual-MiniLM-L12-v2`) measured German
  0.47 → 0.73 but English 0.77 → 0.71, vector side only.
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

## Deployment: release images, a manual deploy over SSH, Caddy in front

**Decision.**

- CI pushes two images per release to ghcr.io: the app (`runtime`, also the worker) and the proxy
  (Caddy). The server builds nothing and has no checkout: `deploy/compose.prod.yml` plus a `.env`.
- Deploys are manual (`.github/workflows/deploy.yml`, a version or `latest`). The workflow drives
  `deploy/deploy.sh` over SSH, with a key whose forced command is that script and whose only
  permitted tunnel is the server's loopback Postgres: `prepare` (stage the compose file, pull,
  start Postgres), `backup` (`pg_dump`, kept on the server), then CI applies the extensions and
  migrations through the tunnel (`deploy/migrate.sh`, Atlas in a container on the runner), then
  `start` (switch, record, check over HTTPS).
- Caddy terminates TLS with a Let's Encrypt certificate for the server's IP address (the
  `shortlived` profile, 6 days, renewed automatically), requires Basic auth on everything except
  `/health`, rate-limits per client (30 requests per 10 s) and caps bodies at 1 MB.

**Why.**

- Caddy over nginx: it obtains and renews certificates by itself, so the whole proxy ships as an
  image with each release; nginx would need certbot and renewal hooks set up on the host.
- An IP certificate because there is no domain; Let's Encrypt has issued them since January 2026.
- Basic auth because client data is personal data and the API had no authentication; it is also
  the "credentials to the API" the assignment asks for.
- Rate limiting before auth: bcrypt is slow on purpose, and each search costs an embedding.
- Images pulled, not built on the server: the release that was tested in CI is the one that runs,
  and the server needs neither the source nor build resources.
- Migrations from CI: the server runs no Atlas, and a failed migration stops the deploy before the
  new release starts. Backups stay on the server so no client data goes to GitHub.

**Trade-offs.**

- The rate limiter is a third-party Caddy module (`caddy-ratelimit`), built in with `xcaddy` and
  pinned to a commit; `just prod-smoke` checks it still answers 429.
- IP certificates last 6 days: ports 80/443 must stay open for renewals, and a changed IP needs a
  redeploy. Clients send no SNI to an IP, hence `default_sni`.
- One instance: a deploy restarts the api (a few seconds of errors).
- Migrations only go forward, and they run while the previous release still serves: a migration
  must not break it (add before you drop). Rolling back across one means restoring the backup
  taken before it; backups stay on the server, not off-site.
- GitHub holds the database password (for the migrations) and a key that can tunnel to Postgres.
- One shared Basic auth credential, no per-user accounts or audit trail.
- No advisors or tenants, although the assignment describes a platform for advisors. Scoping
  clients by advisor later needs an `advisor_id` on clients and chunks, the unique email index
  made per advisor, and a filter in both searches. Filtering after the global nearest-200 chunk
  scan would leave a small advisor with few or no vector matches, so the vector side would need
  pgvector's iterative index scans (`hnsw.iterative_scan`) or a per-advisor index.

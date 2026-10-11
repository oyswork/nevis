"""Document search in Postgres: vector and full-text rankings fused by RRF in one statement.

Vector side: the nearest chunks by cosine distance (HNSW), the best chunk per document. Full-text
side: documents containing any of the query's words. Reciprocal rank fusion (k=60) merges the two
rankings without comparing their scores, which aren't comparable. An optional cross-encoder
reranks the fused candidates in the app.
"""

import re
from dataclasses import dataclass

from nevis.db import DbPool
from nevis.documents.schemas import DocumentPreview
from nevis.embeddings import Embedder, Reranker
from nevis.search.schemas import DocumentHit

# Documents kept from each ranking, and after fusion (what a reranker gets to look at).
RANKED_DOCUMENTS = 50
CANDIDATES = 20
# Chunks fetched from the index: a document often has several, so 4 per ranked document.
NEAREST_CHUNKS = 4 * RANKED_DOCUMENTS
# RRF's damping constant; 60 is the value from the paper and what most implementations use.
RRF_K = 60
# Caps how many full-text matches are scored: ts_rank_cd costs a detoast and a scan per match,
# and one frequent word under OR semantics matches most of the table. Trade-off: with very
# frequent terms the top RANKED_DOCUMENTS are ranked from an arbitrary 1000 matches, not from all
# of them.
TEXT_MATCHES_RANKED = 1000
# ts_rank_cd normalization 2: the rank divided by the document's length, so a long document that
# repeats a common query word doesn't outrank a short one about it. Measured on 58 queries over
# 1,102 documents (scripts/relevance.py): MRR 0.60 -> 0.62, finance queries 0.43 -> 0.53.
TEXT_RANK_NORMALIZATION = 2

# Characters that mean something to websearch_to_tsquery / tsquery. They become spaces: its
# negation handling can keep a "-" inside a word, and long runs of them overflow its parser stack
# ("tsquery stack too small").
TSQUERY_OPERATORS = re.compile(r"""[-!"()<>:*&|'\\]""")

# The words' OR, from plainto_tsquery per word: a document with any of them matches. $1: the words
# (query_words). Guaranteed: the words contain none of TSQUERY_OPERATORS, so plainto_tsquery
# (which has no operator syntax anyway) sees plain text and each piece is the canonical text of
# one small valid tsquery with no negation; the pieces are parenthesized and joined with ' | ',
# which always parses. Words that parse to nothing (stop words) are dropped; with none left the
# result is the empty tsquery, which matches nothing. '@' and '.' stay, so an email is one token.
OR_TSQUERY = """
    coalesce((
        SELECT string_agg('(' || piece || ')', ' | ')
        FROM (
            SELECT plainto_tsquery('english', word)::text AS piece
            FROM unnest($1::text[]) AS word
        ) AS pieces
        WHERE piece <> ''
    ), '')::tsquery
"""

# What a hit shows of each document: no content, which can be 100k characters.
PREVIEW_COLUMNS = tuple(DocumentPreview.model_fields)
PREVIEW_COLUMNS_OF_D = ", ".join(f"d.{column}" for column in PREVIEW_COLUMNS)

# $1: the query's words (OR_TSQUERY), $2: its embedding. `nearest` is a bare ORDER BY ... LIMIT
# so the HNSW index serves it; picking the best chunk per document must stay outside it
# (DISTINCT ON inside would scan everything). A document found only by full text gets its
# closest chunk through the LATERAL join (ordering one document's few chunks). Documents
# without chunks (they predate them, see `just reembed`) are excluded from the text side
# before fusion, so they can't use up its slots.
SEARCH_DOCUMENTS = f"""
    WITH
    query AS MATERIALIZED (SELECT {OR_TSQUERY} AS tsquery),
    nearest AS (
        SELECT document_id, text, embedding <=> $2::vector AS distance
        FROM document_chunks
        ORDER BY embedding <=> $2::vector
        LIMIT {NEAREST_CHUNKS}
    ),
    vector_hits AS (
        SELECT document_id, text, row_number() OVER (ORDER BY distance, document_id) AS rank
        FROM (
            SELECT DISTINCT ON (document_id) document_id, text, distance
            FROM nearest
            ORDER BY document_id, distance
        ) AS best
        ORDER BY rank
        LIMIT {RANKED_DOCUMENTS}
    ),
    text_hits AS (
        SELECT document_id, row_number() OVER (ORDER BY score DESC, document_id) AS rank
        FROM (
            SELECT matches.id AS document_id,
                   ts_rank_cd(matches.tsv, query.tsquery, {TEXT_RANK_NORMALIZATION}) AS score
            FROM (
                SELECT d.id, d.tsv
                FROM documents AS d, query
                WHERE d.tsv @@ query.tsquery
                  AND EXISTS (SELECT 1 FROM document_chunks AS c WHERE c.document_id = d.id)
                LIMIT {TEXT_MATCHES_RANKED}
            ) AS matches, query
            ORDER BY score DESC, matches.id
            LIMIT {RANKED_DOCUMENTS}
        ) AS top
    ),
    fused AS (
        SELECT document_id,
               coalesce(1.0 / ({RRF_K} + v.rank), 0)
                   + coalesce(1.0 / ({RRF_K} + t.rank), 0) AS score
        FROM vector_hits AS v
        FULL JOIN text_hits AS t USING (document_id)
        ORDER BY score DESC, document_id
        LIMIT {CANDIDATES}
    )
    SELECT {PREVIEW_COLUMNS_OF_D}, coalesce(v.text, closest.text) AS matched_text
    FROM fused AS f
    JOIN documents AS d ON d.id = f.document_id
    LEFT JOIN vector_hits AS v ON v.document_id = f.document_id
    LEFT JOIN LATERAL (
        SELECT c.text
        FROM document_chunks AS c
        WHERE c.document_id = f.document_id AND v.document_id IS NULL
        ORDER BY c.embedding <=> $2::vector
        LIMIT 1
    ) AS closest ON true
    ORDER BY f.score DESC, d.id
"""  # noqa: S608 - constants only


@dataclass(frozen=True)
class Candidate:
    document: DocumentPreview
    matched_text: str

    @property
    def rerank_text(self) -> str:
        return f"{self.document.title}: {self.matched_text}"


async def search_documents(
    pool: DbPool, embedder: Embedder, reranker: Reranker | None, q: str, limit: int
) -> list[DocumentHit]:
    """The documents best matching `q`, at most `limit`: fused order, or reranked if configured."""
    if limit == 0:
        return []
    vector = await embedder.embed_query(q)  # CPU-bound: no connection held meanwhile
    candidates = await _candidates(pool, q, vector)
    if reranker is not None and candidates:
        scores = await reranker.rerank(q, [candidate.rerank_text for candidate in candidates])
        ranked = sorted(zip(scores, candidates, strict=True), key=lambda pair: -pair[0])
        candidates = [candidate for _, candidate in ranked]
    return [
        DocumentHit(document=candidate.document, matched_text=candidate.matched_text)
        for candidate in candidates[:limit]
    ]


def query_words(q: str) -> list[str]:
    """The query's words, with tsquery operator characters turned into word breaks."""
    return TSQUERY_OPERATORS.sub(" ", q).split()


async def _candidates(pool: DbPool, q: str, vector: list[float]) -> list[Candidate]:
    rows = await pool.fetch(SEARCH_DOCUMENTS, query_words(q), vector)
    return [
        Candidate(
            document=DocumentPreview(**{key: row[key] for key in PREVIEW_COLUMNS}),
            matched_text=row["matched_text"],
        )
        for row in rows
    ]

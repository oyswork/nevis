"""Search across clients and documents, all in Postgres (see search/clients.py and documents.py).

Clients: whole-word matches, then part-of-a-word matches (trigram `LIKE`), then typo matches
(`word_similarity`). Documents: full-text and semantic (pgvector) search over chunks fused by
reciprocal rank fusion, one hit per document, optionally reranked. Everything is searchable as
soon as its write commits. Clients and documents are ranked separately and listed in that order:
semantic scores can't be compared with trigram ones and are never low enough to cut off
(decisions.md).
"""

import asyncio

from nevis.db import DbPool
from nevis.embeddings import Embedder, Reranker
from nevis.search.clients import search_clients
from nevis.search.documents import search_documents
from nevis.search.schemas import SearchHit


async def search(
    pool: DbPool,
    embedder: Embedder,
    reranker: Reranker | None,
    q: str,
    clients_limit: int,
    documents_limit: int,
) -> list[SearchHit]:
    clients, documents = await asyncio.gather(
        search_clients(pool, q, clients_limit),
        search_documents(pool, embedder, reranker, q, documents_limit),
    )
    return [*clients, *documents]

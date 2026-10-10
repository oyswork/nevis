"""Search across clients and documents: one Meilisearch round trip, results read from Postgres.

Clients: keyword matches first, then substring matches (the experimental CONTAINS filter, see
decisions.md). Documents: hybrid keyword + semantic search over chunks, one hit per document.
Clients and documents are ranked separately and listed in that order: semantic scores can't be
compared with keyword ones and are never low enough to cut off (decisions.md).
"""

import asyncio
import uuid
from collections.abc import Iterable, Sequence

from meilisearch_python_sdk import AsyncClient
from meilisearch_python_sdk.models.search import Hybrid, SearchParams

from nevis.clients.schemas import ClientRead
from nevis.db import DbPool
from nevis.db_models import Client, Document
from nevis.documents.schemas import DocumentRead
from nevis.search.indexes import (
    CLIENT_TEXT_FIELDS,
    CLIENTS_INDEX,
    DOCUMENT_CHUNKS_INDEX,
    DOCUMENT_EMBEDDER,
)
from nevis.search.schemas import ClientHit, DocumentHit, SearchHit

# Share of the semantic side in document ranking; keyword matches still count (decisions.md).
SEMANTIC_RATIO = 0.5
# Shorter substrings match nearly every client.
SUBSTRING_MIN_LENGTH = 3

KEYWORD_CLIENTS = "keyword_clients"
SUBSTRING_CLIENTS = "substring_clients"
DOCUMENTS = "documents"

SELECT_CLIENTS = "SELECT * FROM clients WHERE id = ANY($1::uuid[])"
SELECT_DOCUMENTS = "SELECT * FROM documents WHERE id = ANY($1::uuid[])"


def filter_string(value: str) -> str:
    """A Meilisearch filter string literal: user input can't escape into the expression."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def merge_ids(ranked: Iterable[Iterable[str]], limit: int) -> list[str]:
    """The ids in order of first appearance across the rankings, at most `limit`."""
    merged = dict.fromkeys(id_ for ids in ranked for id_ in ids)
    return list(merged)[:limit]


def build_queries(q: str, clients_limit: int, documents_limit: int) -> dict[str, SearchParams]:
    """The queries to run, by role; a role is left out when it can't match meaningfully."""
    queries = {
        DOCUMENTS: SearchParams(
            index_uid=DOCUMENT_CHUNKS_INDEX,
            query=q,
            limit=documents_limit,
            hybrid=Hybrid(semantic_ratio=SEMANTIC_RATIO, embedder=DOCUMENT_EMBEDDER),
            attributes_to_retrieve=["document_id", "text"],
        )
    }
    # Text without a single word (e.g. "--") would be a placeholder search: every client.
    if any(character.isalnum() for character in q):
        queries[KEYWORD_CLIENTS] = SearchParams(
            index_uid=CLIENTS_INDEX, query=q, limit=clients_limit, attributes_to_retrieve=["id"]
        )
    if len(q) >= SUBSTRING_MIN_LENGTH:
        literal = filter_string(q)
        queries[SUBSTRING_CLIENTS] = SearchParams(
            index_uid=CLIENTS_INDEX,
            query="",
            filter=" OR ".join(f"{field} CONTAINS {literal}" for field in CLIENT_TEXT_FIELDS),
            limit=clients_limit,
            attributes_to_retrieve=["id"],
        )
    return queries


async def search(
    pool: DbPool, search_client: AsyncClient, q: str, clients_limit: int, documents_limit: int
) -> list[SearchHit]:
    queries = build_queries(q, clients_limit, documents_limit)
    results = await search_client.multi_search(list(queries.values()))
    if not isinstance(results, list):  # federated results only come back for a federation
        msg = "expected one result list per query"
        raise TypeError(msg)
    hits = {role: result.hits for role, result in zip(queries, results, strict=True)}
    client_ids = merge_ids(
        (
            [hit["id"] for hit in hits.get(role, [])]
            for role in (KEYWORD_CLIENTS, SUBSTRING_CLIENTS)
        ),
        clients_limit,
    )
    matched_text = {hit["document_id"]: hit["text"] for hit in hits[DOCUMENTS]}
    clients, documents = await asyncio.gather(
        _read(pool, SELECT_CLIENTS, client_ids, Client),
        _read(pool, SELECT_DOCUMENTS, list(matched_text), Document),
    )
    return [
        *(ClientHit(client=ClientRead.model_validate(client)) for client in clients),
        *(
            DocumentHit(
                document=DocumentRead.model_validate(document),
                matched_text=matched_text[str(document.id)],
            )
            for document in documents
        ),
    ]


async def _read[Row: (Client, Document)](
    pool: DbPool, query: str, ids: Sequence[str], model: type[Row]
) -> list[Row]:
    """The rows in the order of `ids`; ids whose rows are gone (not yet reindexed) are skipped."""
    wanted = [uuid.UUID(id_) for id_ in ids]
    rows = {row["id"]: model(**row) for row in await pool.fetch(query, wanted)}
    return [rows[id_] for id_ in wanted if id_ in rows]

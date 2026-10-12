from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import StringConstraints

from nevis.db import DATABASE_UNAVAILABLE_RESPONSES, PoolDep
from nevis.embeddings import EmbedderDep, RerankerDep
from nevis.search import service
from nevis.search.schemas import SearchHit
from nevis.validation import NO_NUL_PATTERN

router = APIRouter(tags=["search"])

QUERY_MAX_LENGTH = 200
CLIENTS_MAX_LIMIT = 50
DOCUMENTS_MAX_LIMIT = 20

SearchQuery = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=QUERY_MAX_LENGTH, pattern=NO_NUL_PATTERN
    ),
    Query(
        description="Words, a name, an email or part of one, or a topic",
        examples=["NevisWealth", "wealth", "address proof"],
    ),
]


@router.get("/search", responses=DATABASE_UNAVAILABLE_RESPONSES)
async def search(
    q: SearchQuery,
    pool: PoolDep,
    embedder: EmbedderDep,
    reranker: RerankerDep,
    clients_limit: Annotated[int, Query(ge=0, le=CLIENTS_MAX_LIMIT)] = 10,
    documents_limit: Annotated[int, Query(ge=0, le=DOCUMENTS_MAX_LIMIT)] = 5,
) -> list[SearchHit]:
    """Clients matching by name, email or description, then documents matching by content.

    Clients: whole-word matches first, then ones containing the text inside a word, then close
    misspellings, regardless of case, accents and script. Queries under 3 characters only match
    whole words. Documents: by meaning as well as words (a document can match without sharing a
    word with the query), the closest first; there is always some closest document, so
    `documents_limit` bounds the list rather than a relevance cutoff.
    Changes are searchable as soon as they are saved.
    """
    return await service.search(pool, embedder, reranker, q, clients_limit, documents_limit)

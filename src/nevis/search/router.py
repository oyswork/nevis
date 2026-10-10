from typing import Annotated, Any

from fastapi import APIRouter, Query, status
from pydantic import StringConstraints

from nevis.db import DATABASE_UNAVAILABLE_DETAIL, PoolDep
from nevis.search import service
from nevis.search.client import SEARCH_UNAVAILABLE_DETAIL, SearchClientDep
from nevis.search.schemas import SearchHit
from nevis.validation import NO_NUL_PATTERN

router = APIRouter(tags=["search"])

QUERY_MAX_LENGTH = 200
CLIENTS_MAX_LIMIT = 50
DOCUMENTS_MAX_LIMIT = 20

# Any: as in nevis.db (FastAPI's free-form OpenAPI objects).
SEARCH_FAILURE_RESPONSES: dict[int | str, dict[str, Any]] = {
    status.HTTP_503_SERVICE_UNAVAILABLE: {
        "description": f"{SEARCH_UNAVAILABLE_DETAIL} or {DATABASE_UNAVAILABLE_DETAIL.lower()}"
    }
}

SearchQuery = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=QUERY_MAX_LENGTH, pattern=NO_NUL_PATTERN
    ),
    Query(description="Words, a name, an email or part of one, or a topic"),
]


@router.get("/search", responses=SEARCH_FAILURE_RESPONSES)
async def search(
    q: SearchQuery,
    pool: PoolDep,
    search_client: SearchClientDep,
    clients_limit: Annotated[int, Query(ge=0, le=CLIENTS_MAX_LIMIT)] = 10,
    documents_limit: Annotated[int, Query(ge=0, le=DOCUMENTS_MAX_LIMIT)] = 5,
) -> list[SearchHit]:
    """Clients matching by name, email or description, then documents matching by content.

    Clients: keyword matches first, then ones containing the text inside a word ("wealth" finds
    john.doe@neviswealth.com), regardless of case and accents. Documents: by meaning as well as
    words ("address proof" finds a utility bill), the closest first; there is always some
    closest document, so `documents_limit` bounds the list rather than a relevance cutoff.
    Changes become searchable within about a second of being saved.
    """
    return await service.search(pool, search_client, q, clients_limit, documents_limit)

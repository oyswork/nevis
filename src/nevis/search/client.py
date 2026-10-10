import logging
from typing import Annotated, Any

from fastapi import Depends, Request, status
from fastapi.responses import JSONResponse
from httpx2 import TimeoutException
from meilisearch_python_sdk import AsyncClient
from meilisearch_python_sdk.errors import MeilisearchCommunicationError

from nevis.config import SearchSettings

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS = 5

SEARCH_UNAVAILABLE_DETAIL = "Search unavailable"
# Failures to reach Meilisearch. The SDK wraps connection errors but lets httpx2 timeouts through.
SEARCH_UNAVAILABLE_ERRORS: tuple[type[Exception], ...] = (
    MeilisearchCommunicationError,
    TimeoutException,
)
# OpenAPI `responses` entry for routes that query Meilisearch (Any: as in nevis.db).
SEARCH_UNAVAILABLE_RESPONSES: dict[int | str, dict[str, Any]] = {
    status.HTTP_503_SERVICE_UNAVAILABLE: {"description": SEARCH_UNAVAILABLE_DETAIL}
}


def create_search_client(settings: SearchSettings) -> AsyncClient:
    # No I/O here: the client connects on first request, so the app can start before Meilisearch.
    return AsyncClient(
        str(settings.meilisearch_url),
        settings.meilisearch_api_key.get_secret_value(),
        timeout=REQUEST_TIMEOUT_SECONDS,
    )


def get_search_client(request: Request) -> AsyncClient:
    search_client: AsyncClient = request.app.state.search_client
    return search_client


SearchClientDep = Annotated[AsyncClient, Depends(get_search_client)]


async def search_unavailable_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.warning("Search unavailable: %s %s", request.method, request.url.path, exc_info=exc)
    return JSONResponse(
        {"detail": SEARCH_UNAVAILABLE_DETAIL}, status_code=status.HTTP_503_SERVICE_UNAVAILABLE
    )

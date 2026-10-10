from typing import Any, Literal

from fastapi import APIRouter, status
from pydantic import BaseModel

from nevis.db import DATABASE_UNAVAILABLE_DETAIL, PoolDep
from nevis.search.client import SEARCH_UNAVAILABLE_DETAIL, SearchClientDep

router = APIRouter(tags=["health"])

# Any: as in nevis.db (FastAPI's free-form OpenAPI objects).
READINESS_FAILURE_RESPONSES: dict[int | str, dict[str, Any]] = {
    status.HTTP_503_SERVICE_UNAVAILABLE: {
        "description": f"{DATABASE_UNAVAILABLE_DETAIL} or {SEARCH_UNAVAILABLE_DETAIL.lower()}"
    }
}


class HealthResponse(BaseModel):
    status: Literal["ok"]


@router.get("/health")
async def health() -> HealthResponse:
    """Liveness: the process is serving requests. Deliberately independent of the database."""
    return HealthResponse(status="ok")


@router.get("/health/ready", responses=READINESS_FAILURE_RESPONSES)
async def ready(pool: PoolDep, search_client: SearchClientDep) -> HealthResponse:
    """Readiness: the database and Meilisearch are reachable. Connection failures become 503s."""
    await pool.fetchval("SELECT 1")
    await search_client.health()
    return HealthResponse(status="ok")

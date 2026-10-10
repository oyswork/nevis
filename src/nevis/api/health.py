from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from nevis.db import DATABASE_UNAVAILABLE_RESPONSES, PoolDep

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: Literal["ok"]


@router.get("/health")
async def health() -> HealthResponse:
    """Liveness: the process is serving requests. Deliberately independent of the database."""
    return HealthResponse(status="ok")


@router.get("/health/ready", responses=DATABASE_UNAVAILABLE_RESPONSES)
async def ready(pool: PoolDep) -> HealthResponse:
    """Readiness: the database is reachable. Connection failures become 503s."""
    await pool.fetchval("SELECT 1")
    return HealthResponse(status="ok")

import logging
from typing import Annotated, Any

import asyncpg
from fastapi import Depends, Request, status
from fastapi.responses import JSONResponse

from nevis.config import Settings

logger = logging.getLogger(__name__)

POOL_MAX_SIZE = 10
CONNECT_TIMEOUT_SECONDS = 5
# Bounds every statement, so single-statement pool calls (pool.fetchrow etc.) hold a connection
# at most this long and requests waiting for a free one don't queue forever.
COMMAND_TIMEOUT_SECONDS = 10

DATABASE_UNAVAILABLE_DETAIL = "Database unavailable"
# Failures to reach Postgres, as opposed to errors in a statement (those stay 500s). OSError
# covers refused connections, DNS failures and timeouts (TimeoutError is an OSError); the app
# does no other I/O that could raise it.
DATABASE_UNAVAILABLE_ERRORS: tuple[type[Exception], ...] = (
    OSError,
    asyncpg.PostgresConnectionError,
    asyncpg.CannotConnectNowError,
    asyncpg.TooManyConnectionsError,
)
# OpenAPI `responses` entry for routes that touch the database. Any: matches the type of
# FastAPI's `responses` parameter, whose values are free-form OpenAPI objects.
DATABASE_UNAVAILABLE_RESPONSES: dict[int | str, dict[str, Any]] = {
    status.HTTP_503_SERVICE_UNAVAILABLE: {"description": DATABASE_UNAVAILABLE_DETAIL}
}

# A `type` alias is evaluated lazily: asyncpg.Pool is generic only in asyncpg-stubs,
# and subscripting it at runtime raises TypeError.
type DbPool = asyncpg.Pool[asyncpg.Record]


async def create_pool(settings: Settings) -> DbPool:
    # min_size=0 opens connections on first use, so the app can start before the database is up.
    return await asyncpg.create_pool(
        str(settings.database_url),
        min_size=0,
        max_size=POOL_MAX_SIZE,
        timeout=CONNECT_TIMEOUT_SECONDS,
        command_timeout=COMMAND_TIMEOUT_SECONDS,
    )


def get_pool(request: Request) -> DbPool:
    # Hand out the pool, not a connection: FastAPI resolves dependencies before it rejects an
    # invalid body, so acquiring here would check out a connection for every 422 response.
    pool: DbPool = request.app.state.pool
    return pool


PoolDep = Annotated[DbPool, Depends(get_pool)]


async def database_unavailable_handler(request: Request, exc: Exception) -> JSONResponse:
    # The client gets a generic message; the cause (host, errno) goes only to the log.
    logger.warning("Database unavailable: %s %s", request.method, request.url.path, exc_info=exc)
    return JSONResponse(
        {"detail": DATABASE_UNAVAILABLE_DETAIL}, status_code=status.HTTP_503_SERVICE_UNAVAILABLE
    )

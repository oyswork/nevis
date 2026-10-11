import logging
from typing import Annotated, Any

import asyncpg
from fastapi import Depends, Request, status
from fastapi.responses import JSONResponse
from pgvector import Vector

from nevis.config import Settings

logger = logging.getLogger(__name__)

POOL_MAX_SIZE = 10
CONNECT_TIMEOUT_SECONDS = 5
# Bounds every statement, so single-statement pool calls (pool.fetchrow etc.) hold a connection
# at most this long and requests waiting for a free one don't queue forever.
COMMAND_TIMEOUT_SECONDS = 10

DATABASE_UNAVAILABLE_DETAIL = "Database unavailable"
DATABASE_TIMEOUT_DETAIL = "Database timed out"
# Failures to reach Postgres, as opposed to errors in a statement (those stay 500s). OSError
# covers refused connections and DNS failures; the app does no other I/O that could raise it.
DATABASE_UNAVAILABLE_ERRORS: tuple[type[Exception], ...] = (
    OSError,
    asyncpg.PostgresConnectionError,
    asyncpg.CannotConnectNowError,
    asyncpg.TooManyConnectionsError,
)
# OpenAPI `responses` entry for routes that touch the database. Any: matches the type of
# FastAPI's `responses` parameter, whose values are free-form OpenAPI objects.
DATABASE_UNAVAILABLE_RESPONSES: dict[int | str, dict[str, Any]] = {
    status.HTTP_503_SERVICE_UNAVAILABLE: {
        "description": f"{DATABASE_UNAVAILABLE_DETAIL} or {DATABASE_TIMEOUT_DETAIL.lower()}"
    }
}

# A `type` alias is evaluated lazily: asyncpg.Pool is generic only in asyncpg-stubs,
# and subscripting it at runtime raises TypeError.
type DbPool = asyncpg.Pool[asyncpg.Record]
# What `async with pool.acquire()` yields; take it to run statements inside a transaction.
type DbConnection = asyncpg.pool.PoolConnectionProxy[asyncpg.Record]


async def register_vector_codec(connection: asyncpg.Connection[asyncpg.Record]) -> None:
    """Read and write pgvector's `vector` as a plain list[float].

    pgvector.asyncpg.register_vector decodes to `Vector` objects, which the generated row
    models (list[float]) reject. Fails with a RuntimeError if the
    extension is missing (docker/initdb/02-extensions.sql).
    """
    try:
        await connection.set_type_codec(
            "vector",
            schema="public",
            encoder=lambda value: Vector(value).to_binary(),
            decoder=lambda data: Vector.from_binary(data).to_list(),
            format="binary",
        )
    except ValueError as exc:  # asyncpg: "unknown type: public.vector"
        msg = (
            "The 'vector' extension is missing from the database: create it with "
            "docker/initdb/02-extensions.sql (it has the command for an existing database)"
        )
        raise RuntimeError(msg) from exc


async def create_pool(settings: Settings) -> DbPool:
    # min_size=0 opens connections on first use, so the app can start before the database is up.
    return await asyncpg.create_pool(
        str(settings.database_url),
        min_size=0,
        max_size=POOL_MAX_SIZE,
        timeout=CONNECT_TIMEOUT_SECONDS,
        command_timeout=COMMAND_TIMEOUT_SECONDS,
        init=register_vector_codec,
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


async def database_timeout_handler(request: Request, exc: Exception) -> JSONResponse:
    # TimeoutError is an OSError, but it usually means a statement outran
    # COMMAND_TIMEOUT_SECONDS (a slow query), not an outage; connecting can also time out.
    # Registered for TimeoutError itself, so it takes precedence over the OSError handler.
    logger.warning("Database timed out: %s %s", request.method, request.url.path, exc_info=exc)
    return JSONResponse(
        {"detail": DATABASE_TIMEOUT_DETAIL}, status_code=status.HTTP_503_SERVICE_UNAVAILABLE
    )

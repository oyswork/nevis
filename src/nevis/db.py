from typing import Annotated

import asyncpg
from fastapi import Depends, Request

from nevis.config import Settings

POOL_MAX_SIZE = 10

# A `type` alias is evaluated lazily: asyncpg.Pool is generic only in asyncpg-stubs,
# and subscripting it at runtime raises TypeError.
type DbPool = asyncpg.Pool[asyncpg.Record]


async def create_pool(settings: Settings) -> DbPool:
    # min_size=0 opens connections on first use, so the app can start before the database is up.
    return await asyncpg.create_pool(str(settings.database_url), min_size=0, max_size=POOL_MAX_SIZE)


def get_pool(request: Request) -> DbPool:
    # Hand out the pool, not a connection: FastAPI resolves dependencies before it rejects an
    # invalid body, so acquiring here would check out a connection for every 422 response.
    pool: DbPool = request.app.state.pool
    return pool


PoolDep = Annotated[DbPool, Depends(get_pool)]

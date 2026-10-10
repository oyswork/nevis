from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from nevis.api.health import router as health_router
from nevis.clients.router import router as clients_router
from nevis.config import Settings
from nevis.db import create_pool


def create_app(settings: Settings | None = None) -> FastAPI:
    """App factory; run with ``uvicorn --factory nevis.main:create_app``."""
    resolved_settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        pool = await create_pool(resolved_settings)
        app.state.pool = pool
        try:
            yield
        finally:
            await pool.close()

    app = FastAPI(title="Nevis Search API", version="0.1.0", lifespan=lifespan)
    app.include_router(health_router)
    app.include_router(clients_router)
    return app

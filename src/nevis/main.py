from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from nevis.api.health import router as health_router
from nevis.clients.router import router as clients_router
from nevis.config import Settings
from nevis.db import (
    DATABASE_UNAVAILABLE_ERRORS,
    create_pool,
    database_timeout_handler,
    database_unavailable_handler,
)
from nevis.documents.router import router as documents_router
from nevis.embeddings import create_embedder, create_reranker
from nevis.outbox.handlers import create_outbox_worker
from nevis.search.client import (
    SEARCH_UNAVAILABLE_ERRORS,
    create_search_client,
    search_unavailable_handler,
)
from nevis.search.router import router as search_router
from nevis.summaries.client import create_summarizer


def create_app(settings: Settings | None = None) -> FastAPI:
    """App factory; run with ``uvicorn --factory nevis.main:create_app``."""
    resolved_settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        # Models first: loading them is the slowest step and can fail, with nothing open yet.
        embedder = await create_embedder(resolved_settings)
        reranker = await create_reranker(resolved_settings)
        pool = await create_pool(resolved_settings)
        search_client = create_search_client(resolved_settings)
        summarizer = create_summarizer(resolved_settings)
        app.state.pool = pool
        app.state.search_client = search_client
        app.state.summarizer = summarizer
        app.state.embedder = embedder
        app.state.reranker = reranker
        outbox_worker = create_outbox_worker(pool, search_client, summarizer)
        outbox_worker.start()
        try:
            yield
        finally:
            await outbox_worker.stop()
            if summarizer is not None:
                await summarizer.aclose()
            await search_client.aclose()
            await pool.close()

    app = FastAPI(title="Nevis Search API", version="0.1.0", lifespan=lifespan)
    for error in DATABASE_UNAVAILABLE_ERRORS:
        app.add_exception_handler(error, database_unavailable_handler)
    app.add_exception_handler(TimeoutError, database_timeout_handler)
    for error in SEARCH_UNAVAILABLE_ERRORS:
        app.add_exception_handler(error, search_unavailable_handler)
    app.include_router(health_router)
    app.include_router(clients_router)
    app.include_router(documents_router)
    app.include_router(search_router)
    return app

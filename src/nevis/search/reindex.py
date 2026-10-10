"""Rebuild the search indexes from Postgres: ``python -m nevis.search.reindex`` (``just reindex``).

Empties the indexes (dropping entries whose rows are gone) and enqueues an index job for every
client and document; the api's outbox worker then refills them. Search is incomplete until it
catches up.
"""

import asyncio
import logging

from meilisearch_python_sdk import AsyncClient

from nevis.config import Settings
from nevis.db import DbPool, create_pool
from nevis.outbox.repository import OutboxKind
from nevis.search.client import create_search_client
from nevis.search.indexes import INDEX_SETTINGS

logger = logging.getLogger(__name__)

ENQUEUE_ALL = """
    INSERT INTO outbox (kind, entity_id)
    SELECT $1, id FROM clients
    UNION ALL
    SELECT $2, id FROM documents
"""
CLEAR_TASK_TIMEOUT_MS = 60_000


async def reindex(pool: DbPool, search_client: AsyncClient) -> int:
    """Empty the indexes and enqueue a job per row; returns the number of jobs."""
    for uid in INDEX_SETTINGS:
        task = await search_client.index(uid).delete_all_documents()
        await search_client.wait_for_task(
            task.task_uid, timeout_in_ms=CLEAR_TASK_TIMEOUT_MS, raise_for_status=True
        )
    status = await pool.execute(ENQUEUE_ALL, OutboxKind.INDEX_CLIENT, OutboxKind.INDEX_DOCUMENT)
    return int(status.split()[-1])  # "INSERT 0 <rows>"


async def _main() -> None:
    settings = Settings()
    pool = await create_pool(settings)
    try:
        async with create_search_client(settings) as search_client:
            enqueued = await reindex(pool, search_client)
    finally:
        await pool.close()
    logger.info("Emptied the search indexes and enqueued %d index jobs", enqueued)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    asyncio.run(_main())

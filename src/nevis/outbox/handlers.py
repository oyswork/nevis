from functools import partial

from meilisearch_python_sdk import AsyncClient

from nevis.db import DbPool
from nevis.outbox.repository import OutboxKind
from nevis.outbox.worker import OutboxWorker
from nevis.search.indexing import index_client


def create_outbox_worker(pool: DbPool, search_client: AsyncClient) -> OutboxWorker:
    """The worker with a handler for every job kind."""
    return OutboxWorker(
        pool,
        {OutboxKind.INDEX_CLIENT: partial(index_client, pool, search_client)},
    )

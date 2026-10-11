from functools import partial

from meilisearch_python_sdk import AsyncClient

from nevis.db import DbPool
from nevis.outbox.repository import OutboxKind
from nevis.outbox.worker import JobHandler, OutboxWorker
from nevis.search.indexing import index_client, index_document


def create_outbox_worker(pool: DbPool, search_client: AsyncClient) -> OutboxWorker:
    """The worker with a handler for every job kind."""
    handlers: dict[OutboxKind, JobHandler] = {
        OutboxKind.INDEX_CLIENT: partial(index_client, pool, search_client),
        OutboxKind.INDEX_DOCUMENT: partial(index_document, pool, search_client),
    }
    return OutboxWorker(pool, handlers)

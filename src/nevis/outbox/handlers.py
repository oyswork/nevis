from functools import partial

from meilisearch_python_sdk import AsyncClient

from nevis.db import DbPool
from nevis.outbox.repository import OutboxKind
from nevis.outbox.worker import JobHandler, OutboxWorker
from nevis.search.indexing import index_client, index_document
from nevis.summaries.client import LocalSummarizer
from nevis.summaries.jobs import summarize_document


def create_outbox_worker(
    pool: DbPool, search_client: AsyncClient, summarizer: LocalSummarizer | None = None
) -> OutboxWorker:
    """The worker with a handler for every job kind; summaries only with a summarizer."""
    handlers: dict[OutboxKind, JobHandler] = {
        OutboxKind.INDEX_CLIENT: partial(index_client, pool, search_client),
        OutboxKind.INDEX_DOCUMENT: partial(index_document, pool, search_client),
    }
    if summarizer is not None:
        handlers[OutboxKind.SUMMARIZE_DOCUMENT] = partial(summarize_document, pool, summarizer)
    return OutboxWorker(pool, handlers)

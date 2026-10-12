"""Local embedding and reranking models (fastembed, ONNX on the CPU).

The models run in worker threads so the event loop stays free. The reranker is optional and
off by default (``NEVIS_SEARCH_RERANK``): its model is never loaded unless enabled.
"""

import asyncio
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Annotated

from fastapi import Depends, Request
from fastembed import TextEmbedding
from fastembed.common.types import NumpyArray
from fastembed.rerank.cross_encoder import TextCrossEncoder

from nevis.config import EmbeddingSettings

EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
# Must match the vector(...) column in schema.sql.
EMBEDDING_DIMENSIONS = 384
RERANKER_MODEL = "Xenova/ms-marco-MiniLM-L-6-v2"
# The default spawns a cpu_count-sized thread pool per model, which is slower under concurrency.
ONNX_THREADS = 2
# onnxruntime's memory arena grows to the largest batch: the default of 256 peaks near 1.2 GB
# RSS, 4 stays near 380 MB with no throughput loss.
BATCH_SIZE = 4
# Model calls running at once in each lane (each call already uses ONNX_THREADS); the rest queue
# without holding a thread. Searches have a lane of their own (query embedding and reranking),
# so they never wait behind a long document, which takes seconds to embed.
MAX_DOCUMENT_CALLS = 2
MAX_QUERY_CALLS = 1
# Documents that may wait for the document lane beyond the ones it runs. One more is refused
# (EmbeddingQueueFullError, answered with 503 and Retry-After) rather than queued: at seconds per
# long document, the last in a longer queue would wait minutes with its request open.
MAX_WAITING_DOCUMENTS = 8
# How long a refused upload is told to wait: about one long document's embedding time.
QUEUE_FULL_RETRY_AFTER_SECONDS = 5


class EmbeddingQueueFullError(Exception):
    """Too many documents are waiting to be embedded; retry shortly."""


async def _run[T](executor: ThreadPoolExecutor, function: Callable[[], T]) -> T:
    return await asyncio.get_running_loop().run_in_executor(executor, function)


class _DocumentLane:
    """The document threads, admitting MAX_WAITING_DOCUMENTS calls beyond those running."""

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=MAX_DOCUMENT_CALLS, thread_name_prefix="onnx-documents"
        )
        # Running or waiting. Only the event loop's thread changes it, so it needs no lock.
        self._admitted = 0

    async def run[T](self, function: Callable[[], T]) -> T:
        if self._admitted >= MAX_DOCUMENT_CALLS + MAX_WAITING_DOCUMENTS:
            raise EmbeddingQueueFullError
        self._admitted += 1
        try:
            return await _run(self._executor, function)
        finally:
            self._admitted -= 1


# Their own pools rather than the loop's default executor, which queued calls would otherwise
# fill, stalling everything else that uses it (DNS lookups included). Bound to no event loop.
_documents = _DocumentLane()
_query_executor = ThreadPoolExecutor(max_workers=MAX_QUERY_CALLS, thread_name_prefix="onnx-queries")


class Embedder:
    def __init__(self, model: TextEmbedding) -> None:
        self._model = model

    async def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        """One vector per text, in order. No instruction prefix: it hurt retrieval in tests.

        Raises EmbeddingQueueFullError when MAX_WAITING_DOCUMENTS calls are already waiting.
        """
        if not texts:
            return []
        texts = list(texts)
        return await _documents.run(
            lambda: [
                _to_floats(vector)
                for vector in self._model.passage_embed(texts, batch_size=BATCH_SIZE)
            ],
        )

    async def embed_query(self, text: str) -> list[float]:
        return await _run(
            _query_executor, lambda: _to_floats(next(iter(self._model.query_embed(text))))
        )


class Reranker:
    def __init__(self, model: TextCrossEncoder) -> None:
        self._model = model

    async def rerank(self, query: str, documents: Sequence[str]) -> list[float]:
        """Raw relevance scores (higher is better), in the order of ``documents``."""
        if not documents:
            return []
        documents = list(documents)
        return await _run(
            _query_executor,
            lambda: [
                float(score)
                for score in self._model.rerank(query, documents, batch_size=BATCH_SIZE)
            ],
        )


def _to_floats(vector: NumpyArray) -> list[float]:
    floats: list[float] = vector.tolist()  # Python floats, converted in C
    return floats


def _cache_dir(settings: EmbeddingSettings) -> str | None:
    return None if settings.model_cache_dir is None else str(settings.model_cache_dir)


async def create_embedder(settings: EmbeddingSettings) -> Embedder:
    # Loading blocks (seconds from disk, much longer when it has to download), so not on the loop.
    model = await asyncio.to_thread(
        TextEmbedding, EMBEDDING_MODEL, cache_dir=_cache_dir(settings), threads=ONNX_THREADS
    )
    return Embedder(model)


async def create_reranker(settings: EmbeddingSettings) -> Reranker | None:
    """The reranker, or None unless ``search_rerank`` is on (then its model is never loaded)."""
    if not settings.search_rerank:
        return None
    model = await asyncio.to_thread(
        TextCrossEncoder, RERANKER_MODEL, cache_dir=_cache_dir(settings), threads=ONNX_THREADS
    )
    return Reranker(model)


def get_embedder(request: Request) -> Embedder:
    embedder: Embedder = request.app.state.embedder
    return embedder


def get_reranker(request: Request) -> Reranker | None:
    reranker: Reranker | None = request.app.state.reranker
    return reranker


EmbedderDep = Annotated[Embedder, Depends(get_embedder)]
RerankerDep = Annotated[Reranker | None, Depends(get_reranker)]


def download() -> None:
    """Fetch both models into the cache dir; the Dockerfile bakes them into the image."""
    settings = EmbeddingSettings()
    TextEmbedding(EMBEDDING_MODEL, cache_dir=_cache_dir(settings), threads=ONNX_THREADS)
    TextCrossEncoder(RERANKER_MODEL, cache_dir=_cache_dir(settings), threads=ONNX_THREADS)


if __name__ == "__main__":
    download()

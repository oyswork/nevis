import asyncio
import math
import threading
import time
from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from unittest.mock import NonCallableMagicMock, create_autospec

import numpy as np
import pytest
import pytest_asyncio
from fastembed import TextEmbedding
from fastembed.rerank.cross_encoder import TextCrossEncoder

from nevis.config import EmbeddingSettings, Settings
from nevis.embeddings import (
    BATCH_SIZE,
    EMBEDDING_DIMENSIONS,
    EMBEDDING_MODEL,
    MAX_DOCUMENT_CALLS,
    MAX_QUERY_CALLS,
    MAX_WAITING_DOCUMENTS,
    ONNX_THREADS,
    RERANKER_MODEL,
    Embedder,
    EmbeddingQueueFullError,
    Reranker,
    create_embedder,
    create_reranker,
    download,
)
from nevis.main import create_app


@pytest.fixture
def fake_text_embedding(monkeypatch: pytest.MonkeyPatch) -> NonCallableMagicMock:
    model: NonCallableMagicMock = create_autospec(TextEmbedding, instance=True)
    model.passage_embed.side_effect = lambda texts, **_: iter(
        [np.array([1.0, 2.0], dtype=np.float32) for _ in texts]
    )
    model.query_embed.side_effect = lambda _, **__: iter([np.array([3.0, 4.0], dtype=np.float32)])
    constructor: NonCallableMagicMock = create_autospec(TextEmbedding, return_value=model)
    monkeypatch.setattr("nevis.embeddings.TextEmbedding", constructor)
    return constructor


@pytest.fixture
def fake_cross_encoder(monkeypatch: pytest.MonkeyPatch) -> NonCallableMagicMock:
    model: NonCallableMagicMock = create_autospec(TextCrossEncoder, instance=True)
    model.rerank.side_effect = lambda _, documents, **__: iter(
        [np.float32(len(d)) for d in documents]
    )
    constructor: NonCallableMagicMock = create_autospec(TextCrossEncoder, return_value=model)
    monkeypatch.setattr("nevis.embeddings.TextCrossEncoder", constructor)
    return constructor


@pytest.mark.unit
async def test_create_embedder_loads_the_model_with_threads_and_cache_dir(
    fake_text_embedding: NonCallableMagicMock, tmp_path: Path
) -> None:
    await create_embedder(EmbeddingSettings(model_cache_dir=tmp_path))

    fake_text_embedding.assert_called_once_with(
        EMBEDDING_MODEL, cache_dir=str(tmp_path), threads=ONNX_THREADS
    )


@pytest.mark.unit
async def test_create_embedder_uses_the_library_cache_when_no_dir_is_set(
    fake_text_embedding: NonCallableMagicMock,
) -> None:
    await create_embedder(EmbeddingSettings(model_cache_dir=None))

    fake_text_embedding.assert_called_once_with(
        EMBEDDING_MODEL, cache_dir=None, threads=ONNX_THREADS
    )


@pytest.mark.unit
async def test_embed_passages_returns_float_lists_in_input_order(
    fake_text_embedding: NonCallableMagicMock,
) -> None:
    embedder = await create_embedder(EmbeddingSettings())

    vectors = await embedder.embed_passages(["a", "b", "c"])

    assert vectors == [[1.0, 2.0]] * 3
    assert all(type(value) is float for vector in vectors for value in vector)
    model = fake_text_embedding.return_value
    model.passage_embed.assert_called_once_with(["a", "b", "c"], batch_size=BATCH_SIZE)


@pytest.mark.unit
async def test_embed_passages_of_nothing_returns_nothing_without_running_the_model(
    fake_text_embedding: NonCallableMagicMock,
) -> None:
    embedder = await create_embedder(EmbeddingSettings())

    assert await embedder.embed_passages([]) == []
    fake_text_embedding.return_value.passage_embed.assert_not_called()


@pytest.mark.unit
async def test_embed_query_returns_one_float_list(
    fake_text_embedding: NonCallableMagicMock,
) -> None:
    embedder = await create_embedder(EmbeddingSettings())

    vector = await embedder.embed_query("proof of address")

    assert vector == [3.0, 4.0]
    assert all(type(value) is float for value in vector)
    fake_text_embedding.return_value.query_embed.assert_called_once_with("proof of address")


class PeakCounter:
    """Wraps model calls to record how many ran at once."""

    def __init__(self) -> None:
        self.peak = 0
        self._running = 0
        self._lock = threading.Lock()

    def slow_call[T](self, result: T) -> Callable[..., T]:
        def call(*_: object, **__: object) -> T:
            with self._lock:
                self._running += 1
                self.peak = max(self.peak, self._running)
            time.sleep(0.05)
            with self._lock:
                self._running -= 1
            return result

        return call


@pytest.mark.unit
async def test_document_embedding_runs_at_most_max_document_calls_at_once(
    fake_text_embedding: NonCallableMagicMock,
) -> None:
    calls = PeakCounter()
    fake_text_embedding.return_value.passage_embed.side_effect = calls.slow_call(
        [np.array([1.0], dtype=np.float32)]
    )
    embedder = await create_embedder(EmbeddingSettings())

    await asyncio.gather(*(embedder.embed_passages(["d"]) for _ in range(4)))

    assert calls.peak == MAX_DOCUMENT_CALLS


@pytest.mark.unit
async def test_query_embedding_and_reranking_share_max_query_calls(
    fake_text_embedding: NonCallableMagicMock,
    fake_cross_encoder: NonCallableMagicMock,
) -> None:
    calls = PeakCounter()
    fake_text_embedding.return_value.query_embed.side_effect = calls.slow_call(
        [np.array([1.0], dtype=np.float32)]
    )
    fake_cross_encoder.return_value.rerank.side_effect = calls.slow_call([1.0])
    embedder = await create_embedder(EmbeddingSettings())
    reranker = await create_reranker(EmbeddingSettings(search_rerank=True))
    assert reranker is not None

    await asyncio.gather(
        *(embedder.embed_query("q") for _ in range(3)),
        *(reranker.rerank("q", ["d"]) for _ in range(3)),
    )

    assert calls.peak == MAX_QUERY_CALLS


@pytest.mark.unit
async def test_queries_do_not_wait_behind_document_embedding(
    fake_text_embedding: NonCallableMagicMock,
    fake_cross_encoder: NonCallableMagicMock,
) -> None:
    release = threading.Event()

    def blocked_passage_embed(texts: list[str], **_: object) -> list[np.ndarray]:
        release.wait(timeout=5)
        return [np.array([1.0], dtype=np.float32) for _ in texts]

    fake_text_embedding.return_value.passage_embed.side_effect = blocked_passage_embed
    embedder = await create_embedder(EmbeddingSettings())
    reranker = await create_reranker(EmbeddingSettings(search_rerank=True))
    assert reranker is not None
    # More documents than the document lane runs at once: every one of its threads is busy.
    documents = [
        asyncio.create_task(embedder.embed_passages(["d"])) for _ in range(MAX_DOCUMENT_CALLS + 1)
    ]
    try:
        async with asyncio.timeout(1):
            assert await embedder.embed_query("q") == [3.0, 4.0]
            assert await reranker.rerank("q", ["d"]) == [1.0]
    finally:
        release.set()
        await asyncio.gather(*documents)


@pytest.mark.unit
async def test_documents_beyond_the_waiting_limit_are_refused_without_running_the_model(
    fake_text_embedding: NonCallableMagicMock,
) -> None:
    release = threading.Event()

    def blocked_passage_embed(texts: list[str], **_: object) -> list[np.ndarray]:
        release.wait(timeout=5)
        return [np.array([1.0], dtype=np.float32) for _ in texts]

    model = fake_text_embedding.return_value
    model.passage_embed.side_effect = blocked_passage_embed
    embedder = await create_embedder(EmbeddingSettings())
    # Both threads busy and the queue full.
    admitted = [
        asyncio.create_task(embedder.embed_passages(["admitted"]))
        for _ in range(MAX_DOCUMENT_CALLS + MAX_WAITING_DOCUMENTS)
    ]
    await asyncio.sleep(0)
    try:
        with pytest.raises(EmbeddingQueueFullError):
            await embedder.embed_passages(["refused"])
    finally:
        release.set()
        await asyncio.gather(*admitted)

    assert all(call.args[0] == ["admitted"] for call in model.passage_embed.call_args_list)
    # The queue drained, so there is room again.
    assert await embedder.embed_passages(["later"]) == [[1.0]]


@pytest.mark.unit
async def test_app_opens_no_pool_when_a_model_fails_to_load(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fail_to_load(_: Settings) -> Embedder:
        raise OSError("model files missing")

    create_pool: NonCallableMagicMock = create_autospec(lambda _: None)
    monkeypatch.setattr("nevis.main.create_embedder", fail_to_load)
    monkeypatch.setattr("nevis.main.create_pool", create_pool)
    app = create_app(settings)

    with pytest.raises(OSError, match="model files missing"):
        async with app.router.lifespan_context(app):
            pass

    create_pool.assert_not_called()


@pytest.mark.unit
async def test_create_reranker_is_none_and_loads_nothing_when_disabled(
    fake_cross_encoder: NonCallableMagicMock,
) -> None:
    assert await create_reranker(EmbeddingSettings(search_rerank=False)) is None
    fake_cross_encoder.assert_not_called()


@pytest.mark.unit
async def test_create_reranker_loads_the_cross_encoder_when_enabled(
    fake_cross_encoder: NonCallableMagicMock, tmp_path: Path
) -> None:
    reranker = await create_reranker(
        EmbeddingSettings(search_rerank=True, model_cache_dir=tmp_path)
    )

    assert isinstance(reranker, Reranker)
    fake_cross_encoder.assert_called_once_with(
        RERANKER_MODEL, cache_dir=str(tmp_path), threads=ONNX_THREADS
    )


@pytest.mark.unit
async def test_rerank_returns_float_scores_in_input_order(
    fake_cross_encoder: NonCallableMagicMock,
) -> None:
    reranker = await create_reranker(EmbeddingSettings(search_rerank=True))
    assert reranker is not None

    scores = await reranker.rerank("q", ["a", "abc", "ab"])

    assert scores == [1.0, 3.0, 2.0]
    assert all(type(score) is float for score in scores)
    fake_cross_encoder.return_value.rerank.assert_called_once_with(
        "q", ["a", "abc", "ab"], batch_size=BATCH_SIZE
    )


@pytest.mark.unit
async def test_rerank_of_nothing_returns_nothing_without_running_the_model(
    fake_cross_encoder: NonCallableMagicMock,
) -> None:
    reranker = await create_reranker(EmbeddingSettings(search_rerank=True))
    assert reranker is not None

    assert await reranker.rerank("q", []) == []
    fake_cross_encoder.return_value.rerank.assert_not_called()


@pytest.mark.unit
def test_download_loads_both_models_into_the_cache_dir(
    fake_text_embedding: NonCallableMagicMock,
    fake_cross_encoder: NonCallableMagicMock,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("NEVIS_MODEL_CACHE_DIR", str(tmp_path))

    download()

    fake_text_embedding.assert_called_once_with(
        EMBEDDING_MODEL, cache_dir=str(tmp_path), threads=ONNX_THREADS
    )
    fake_cross_encoder.assert_called_once_with(
        RERANKER_MODEL, cache_dir=str(tmp_path), threads=ONNX_THREADS
    )


@pytest.mark.unit
@pytest.mark.parametrize(("value", "expected"), [("", False), ("true", True), ("false", False)])
def test_search_rerank_setting_reads_the_environment(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: bool
) -> None:
    monkeypatch.setenv("NEVIS_SEARCH_RERANK", value)

    assert EmbeddingSettings().search_rerank is expected


@pytest.mark.unit
def test_empty_model_cache_dir_means_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEVIS_MODEL_CACHE_DIR", "")

    assert EmbeddingSettings().model_cache_dir is None


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def real_reranker() -> AsyncGenerator[Reranker]:
    reranker = await create_reranker(EmbeddingSettings(search_rerank=True))
    assert reranker is not None
    yield reranker


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    return dot / (math.hypot(*a) * math.hypot(*b))


@pytest.mark.integration
async def test_real_embeddings_have_the_schema_dimensions(real_embedder: Embedder) -> None:
    vectors = await real_embedder.embed_passages(["one", "two"])
    query = await real_embedder.embed_query("one")

    assert [len(v) for v in vectors] == [EMBEDDING_DIMENSIONS] * 2
    assert len(query) == EMBEDDING_DIMENSIONS


@pytest.mark.integration
async def test_real_embeddings_rank_a_relevant_passage_above_an_unrelated_one(
    real_embedder: Embedder,
) -> None:
    relevant, unrelated = await real_embedder.embed_passages(
        ["Passport issued by the Republic of Ireland to the client.", "Banana bread recipe."]
    )
    query = await real_embedder.embed_query("identity document")

    assert _cosine(query, relevant) > _cosine(query, unrelated)


@pytest.mark.integration
async def test_real_reranker_scores_a_relevant_passage_above_an_unrelated_one(
    real_reranker: Reranker,
) -> None:
    scores = await real_reranker.rerank(
        "identity document",
        ["Banana bread recipe.", "Passport issued by the Republic of Ireland to the client."],
    )

    assert len(scores) == 2
    assert scores[1] > scores[0]


@pytest.mark.integration
async def test_concurrent_embedding_calls_all_complete(real_embedder: Embedder) -> None:
    results = await asyncio.gather(*(real_embedder.embed_query(f"query {i}") for i in range(6)))

    assert len(results) == 6

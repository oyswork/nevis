"""The document query against a real database, with vectors chosen by the tests.

The embedder is a stub that returns a fixed query vector, so the chunk distances (and so the
rankings) are exact: `basis(0)` is the query, `basis(1)` is at cosine distance 1 from it, and
`nearly(0, 1, weight)` sits between them.
"""

import datetime
import random
import uuid
from unittest.mock import AsyncMock, NonCallableMagicMock, create_autospec

import pytest

from nevis.db import DbPool
from nevis.documents.schemas import DocumentPreview
from nevis.embeddings import EMBEDDING_DIMENSIONS, Reranker
from nevis.search.documents import (
    OR_TSQUERY,
    TSQUERY_OPERATORS,
    Candidate,
    query_words,
    search_documents,
)

INSERT_CHUNK = (
    "INSERT INTO document_chunks (document_id, ordinal, text, embedding) VALUES ($1, $2, $3, $4)"
)
INSERT_FILLER_CHUNK = (
    "INSERT INTO document_chunks (document_id, ordinal, text, embedding) VALUES ($1, 0, $2, $3)"
)


def basis(index: int) -> list[float]:
    return [1.0 if position == index else 0.0 for position in range(EMBEDDING_DIMENSIONS)]


def nearly(index: int, other: int, weight: float) -> list[float]:
    """Mostly `basis(index)`, with `weight` of `basis(other)` mixed in (farther from index 0)."""
    vector = basis(index)
    vector[other] = weight
    return vector


@pytest.fixture
def stub_embedder(fake_embedder: NonCallableMagicMock) -> NonCallableMagicMock:
    """The conftest fake embedder, always answering with `basis(0)`."""
    fake_embedder.embed_query.return_value = basis(0)
    return fake_embedder


@pytest.fixture
async def client_id(db_client: object, db_pool: DbPool) -> uuid.UUID:
    """A client to attach documents to. It depends on `db_client` only for its side effect:
    that fixture empties the database first (the argument itself is unused)."""
    value = await db_pool.fetchval(
        "INSERT INTO clients (first_name, last_name, email) VALUES ('A', 'B', 'a@b.example') "
        "RETURNING id"
    )
    assert isinstance(value, uuid.UUID)
    return value


async def add_document(
    pool: DbPool,
    client_id: uuid.UUID,
    title: str,
    content: str,
    chunks: list[tuple[str, list[float]]],
) -> uuid.UUID:
    document_id = await pool.fetchval(
        "INSERT INTO documents (client_id, title, content) VALUES ($1, $2, $3) RETURNING id",
        client_id,
        title,
        content,
    )
    assert isinstance(document_id, uuid.UUID)
    await pool.executemany(
        INSERT_CHUNK,
        [(document_id, ordinal, text, vector) for ordinal, (text, vector) in enumerate(chunks)],
    )
    return document_id


@pytest.mark.integration
async def test_matched_text_is_the_chunk_closest_to_the_query(
    db_pool: DbPool, client_id: uuid.UUID, stub_embedder: NonCallableMagicMock
) -> None:
    await add_document(
        db_pool,
        client_id,
        "Report",
        "first second",
        [("far chunk", basis(1)), ("near chunk", nearly(0, 1, 0.1)), ("middle chunk", basis(2))],
    )

    (hit,) = await search_documents(db_pool, stub_embedder, None, "anything", 5)

    assert hit.matched_text == "near chunk"


@pytest.mark.integration
async def test_without_a_reranker_documents_keep_their_fused_order(
    db_pool: DbPool, client_id: uuid.UUID, stub_embedder: NonCallableMagicMock
) -> None:
    ids = [
        await add_document(db_pool, client_id, title, title, [(title, nearly(0, 1, weight))])
        for title, weight in (("near", 0.1), ("middle", 1.0), ("far", 5.0))
    ]

    hits = await search_documents(db_pool, stub_embedder, None, "nomatch", 5)

    assert [hit.document.id for hit in hits] == ids


@pytest.mark.integration
async def test_a_document_found_by_both_lists_outranks_one_found_by_one(
    db_pool: DbPool, client_id: uuid.UUID, stub_embedder: NonCallableMagicMock
) -> None:
    # "closer" is the nearest by meaning; "both" is a little farther but also has the word.
    await add_document(db_pool, client_id, "Closer", "unrelated", [("c", basis(0))])
    both = await add_document(
        db_pool, client_id, "Both", "mentions zebra here", [("b", nearly(0, 1, 0.5))]
    )

    hits = await search_documents(db_pool, stub_embedder, None, "zebra", 5)

    assert hits[0].document.id == both


@pytest.mark.integration
async def test_a_reranker_reorders_the_candidates_by_score(
    db_pool: DbPool, client_id: uuid.UUID, stub_embedder: NonCallableMagicMock
) -> None:
    ids = [
        await add_document(db_pool, client_id, title, title, [(f"text of {title}", vector)])
        for title, vector in (("A", basis(0)), ("B", nearly(0, 1, 1.0)), ("C", nearly(0, 1, 5.0)))
    ]
    reranker: NonCallableMagicMock = create_autospec(Reranker, instance=True)
    reranker.rerank.return_value = [0.1, 0.5, 0.9]  # in candidate (fused) order: A, B, C

    hits = await search_documents(db_pool, stub_embedder, reranker, "query", 2)

    assert [hit.document.id for hit in hits] == [ids[2], ids[1]]
    reranker.rerank.assert_awaited_once_with(
        "query", ["A: text of A", "B: text of B", "C: text of C"]
    )


@pytest.mark.integration
async def test_a_full_text_only_match_still_gets_its_closest_chunk(
    db_pool: DbPool, client_id: uuid.UUID, stub_embedder: NonCallableMagicMock
) -> None:
    # 210 chunks nearer to the query than the target's push it out of the vector top 200.
    fillers = [(uuid.uuid4(), f"filler {n}", nearly(0, 2, 0.001 * (n + 1))) for n in range(210)]
    await db_pool.executemany(
        "INSERT INTO documents (id, client_id, title, content) VALUES ($1, $2, $3, 'filler')",
        [(id_, client_id, title) for id_, title, _ in fillers],
    )
    await db_pool.executemany(
        INSERT_FILLER_CHUNK,
        fillers,
    )
    target = await add_document(
        db_pool,
        client_id,
        "Target",
        "a zebra crossing",
        [("far zebra", basis(5)), ("closer zebra", nearly(5, 0, 0.5))],
    )

    hits = await search_documents(db_pool, stub_embedder, None, "zebra", 20)

    (found,) = [hit for hit in hits if hit.document.id == target]
    assert found.matched_text == "closer zebra"


@pytest.mark.integration
async def test_a_document_without_chunks_is_skipped(
    db_pool: DbPool, client_id: uuid.UUID, stub_embedder: NonCallableMagicMock
) -> None:
    await add_document(db_pool, client_id, "Legacy", "a zebra", [])

    assert await search_documents(db_pool, stub_embedder, None, "zebra", 5) == []


def candidate(title: str) -> Candidate:
    document = DocumentPreview(
        id=uuid.uuid4(),
        client_id=uuid.uuid4(),
        title=title,
        summary=None,
        created_at=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
    )
    return Candidate(document=document, matched_text=f"text of {title}")


@pytest.fixture
def fake_candidates(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Replaces the database fetch of `search_documents`; set `return_value` per test."""
    fetch = AsyncMock(return_value=[])
    monkeypatch.setattr("nevis.search.documents._candidates", fetch)
    return fetch


@pytest.mark.unit
async def test_the_query_is_embedded_as_typed(
    fake_pool: NonCallableMagicMock,
    stub_embedder: NonCallableMagicMock,
    fake_candidates: AsyncMock,
) -> None:
    await search_documents(fake_pool, stub_embedder, None, "Résumé Check", 5)

    stub_embedder.embed_query.assert_awaited_once_with("Résumé Check")
    fake_candidates.assert_awaited_once_with(fake_pool, "Résumé Check", basis(0))


@pytest.mark.unit
async def test_a_limit_of_zero_does_no_work(
    fake_pool: NonCallableMagicMock,
    stub_embedder: NonCallableMagicMock,
    fake_candidates: AsyncMock,
) -> None:
    assert await search_documents(fake_pool, stub_embedder, None, "zebra", 0) == []
    stub_embedder.embed_query.assert_not_called()
    fake_candidates.assert_not_called()


@pytest.mark.unit
async def test_a_reranker_is_not_called_without_candidates(
    fake_pool: NonCallableMagicMock,
    stub_embedder: NonCallableMagicMock,
    fake_candidates: AsyncMock,
) -> None:
    reranker: NonCallableMagicMock = create_autospec(Reranker, instance=True)

    assert await search_documents(fake_pool, stub_embedder, reranker, "zebra", 5) == []
    reranker.rerank.assert_not_called()


@pytest.mark.unit
async def test_without_a_reranker_the_fused_order_and_the_limit_are_kept(
    fake_pool: NonCallableMagicMock,
    stub_embedder: NonCallableMagicMock,
    fake_candidates: AsyncMock,
) -> None:
    fake_candidates.return_value = [candidate(title) for title in ("A", "B", "C")]

    hits = await search_documents(fake_pool, stub_embedder, None, "zebra", 2)

    assert [hit.document.title for hit in hits] == ["A", "B"]


@pytest.mark.integration
async def test_a_chunkless_document_does_not_reduce_the_number_of_results(
    db_pool: DbPool, client_id: uuid.UUID, stub_embedder: NonCallableMagicMock
) -> None:
    # 25 legacy documents match the word better than the 20 documents that have chunks.
    for n in range(25):
        await add_document(db_pool, client_id, f"Legacy {n}", "zebra zebra zebra zebra", [])
    for n in range(20):
        await add_document(
            db_pool, client_id, f"Chunked {n}", "zebra", [("c", nearly(0, 1, 0.01 * (n + 1)))]
        )

    hits = await search_documents(db_pool, stub_embedder, None, "zebra", 20)

    assert len(hits) == 20
    assert all(hit.document.title.startswith("Chunked") for hit in hits)


@pytest.mark.integration
async def test_pool_connections_have_the_search_settings(db_pool: DbPool) -> None:
    # First statement on a fresh connection, before pg_trgm or pgvector ran anything there.
    assert await db_pool.fetchval("SHOW pg_trgm.word_similarity_threshold") == "0.5"
    assert await db_pool.fetchval("SHOW hnsw.ef_search") == "200"
    # ... and still after the libraries are loaded and the settings are in use.
    await db_pool.fetchval("SELECT word_similarity('a', 'b')")
    await db_pool.fetchval("SELECT '[1,2]'::vector <=> '[1,3]'::vector")
    assert await db_pool.fetchval("SHOW pg_trgm.word_similarity_threshold") == "0.5"
    assert await db_pool.fetchval("SHOW hnsw.ef_search") == "200"


async def or_query(pool: DbPool, q: str) -> str:
    value = await pool.fetchval(f"SELECT ({OR_TSQUERY})::text", query_words(q))
    assert isinstance(value, str)
    return value


@pytest.mark.unit
@pytest.mark.parametrize(
    ("q", "words"),
    [
        ("passport pension", ["passport", "pension"]),
        ("  a   b\t\nc ", ["a", "b", "c"]),
        ("ab!-aa", ["ab", "aa"]),
        ('foo"-bar', ["foo", "bar"]),
        ("-" * 40, []),
        ("a:* & | (b) <-> 'c' \\d", ["a", "b", "c", "d"]),
        ("john.doe@x.com", ["john.doe@x.com"]),
        ("", []),
    ],
)
def test_query_words_drops_tsquery_operators(q: str, words: list[str]) -> None:
    assert query_words(q) == words


@pytest.mark.integration
async def test_the_full_text_query_ors_the_words(db_pool: DbPool) -> None:
    assert await or_query(db_pool, "passport pension") == "'passport' | 'pension'"


@pytest.mark.integration
async def test_the_full_text_query_treats_hyphens_and_negation_as_word_breaks(
    db_pool: DbPool,
) -> None:
    assert await or_query(db_pool, "well-known tax") == "'well' | 'known' | 'tax'"
    assert await or_query(db_pool, "bill -passport") == "'bill' | 'passport'"


@pytest.mark.integration
async def test_the_full_text_query_keeps_an_email_as_one_token(db_pool: DbPool) -> None:
    assert await or_query(db_pool, "mail john.doe@x.com") == "'mail' | 'john.doe@x.com'"


@pytest.mark.integration
async def test_the_full_text_query_is_empty_without_usable_words(db_pool: DbPool) -> None:
    assert await or_query(db_pool, "the - of !") == ""


NASTY_QUERIES = [
    "",
    " ",
    '"',
    "!",
    ":*",
    "a:*",
    "&",
    "|",
    "a & | b",
    "()",
    "(a",
    "'",
    "\\",
    "-",
    "<->",
    "or",
    "'; DROP TABLE documents; --",
    "x" * 500,
    "😀",
    "a\tb\nc",
    "-" * 40,
    "a " + "-" * 50,
    "ab!-aa",
    'foo"-bar',
    " ".join(f"w{n}" for n in range(100)),
]


@pytest.mark.integration
@pytest.mark.parametrize("q", NASTY_QUERIES)
async def test_the_full_text_query_is_valid_for_any_input(db_pool: DbPool, q: str) -> None:
    text = await or_query(db_pool, q)

    assert "!" not in text
    await db_pool.fetchval(
        f"SELECT to_tsvector('english', 'a b c') @@ ({OR_TSQUERY})", query_words(q)
    )


@pytest.mark.integration
async def test_the_full_text_query_survives_a_fuzz_of_operator_characters(
    db_pool: DbPool,
) -> None:
    generator = random.Random(7)  # noqa: S311 - a reproducible fuzz, not security
    alphabet = [*"-!\"()<>:*&|'\\ ab@.", "or", "and"]
    for _ in range(300):
        q = "".join(generator.choices(alphabet, k=generator.randint(1, 60)))

        text = await or_query(db_pool, q)

        assert "!" not in text
        assert not TSQUERY_OPERATORS.search("".join(query_words(q)))


@pytest.mark.integration
async def test_a_negation_inside_a_word_does_not_exclude_documents(
    db_pool: DbPool, client_id: uuid.UUID, stub_embedder: NonCallableMagicMock
) -> None:
    keep = await add_document(db_pool, client_id, "Keep", "ab aa", [("c", basis(5))])

    hits = await search_documents(db_pool, stub_embedder, None, "ab!-aa", 5)

    assert [hit.document.id for hit in hits] == [keep]


@pytest.mark.integration
async def test_a_long_document_repeating_a_word_does_not_outrank_a_short_one_about_it(
    db_pool: DbPool, client_id: uuid.UUID, stub_embedder: NonCallableMagicMock
) -> None:
    # The vector side puts Long one place above Short, so the text side decides. Unnormalized,
    # Long's 20 matches would win it; divided by length (TEXT_RANK_NORMALIZATION), Short does.
    long_content = " ".join(["zebra"] + [f"filler{n}" for n in range(19)]) * 20
    await add_document(db_pool, client_id, "Nearest", "unrelated", [("c", basis(0))])
    await add_document(db_pool, client_id, "Long", long_content, [("c", nearly(0, 1, 0.2))])
    await add_document(db_pool, client_id, "Short", "zebra", [("c", nearly(0, 1, 0.3))])

    hits = await search_documents(db_pool, stub_embedder, None, "zebra", 3)

    assert [hit.document.title for hit in hits] == ["Short", "Long", "Nearest"]

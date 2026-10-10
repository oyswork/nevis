from collections.abc import AsyncGenerator
from unittest.mock import NonCallableMagicMock

import pytest
from httpx2 import AsyncClient
from meilisearch_python_sdk import AsyncClient as SearchClient
from meilisearch_python_sdk.errors import MeilisearchCommunicationError
from pydantic import TypeAdapter

from nevis.config import Settings
from nevis.db import DbPool, create_pool
from nevis.outbox.handlers import create_outbox_worker
from nevis.search.client import create_search_client
from nevis.search.schemas import ClientHit, DocumentHit, SearchHit
from nevis.search.service import filter_string, merge_ids

JOHN = {
    "first_name": "John",
    "last_name": "Doe",
    "email": "john.doe@neviswealth.com",
    "description": "Prefers sustainable funds",
}
ZOE = {
    "first_name": "Zoë",
    "last_name": "McAllister",
    "email": "Zoe.McAllister@AcmeCapital.co.uk",
    "description": "Retired, interested in ETFs",
}
ANNA = {
    "first_name": "Anna",
    "last_name": "Schmidt",
    "email": "anna@weberfamilyoffice.de",
    "description": "Referred by NevisWealth; works in venture capital",
}
DOCUMENTS = [
    ("Utility bill March", "Electricity utility bill from Vattenfall for 12 Lake Road, Zurich."),
    ("Passport scan", "Scanned copy of the client's passport, valid until 2031."),
    ("Tax return 2025", "Federal income tax return including pension contributions."),
    ("Meeting notes", "Discussed retirement timeline and buying a holiday home."),
]
HITS = TypeAdapter(list[SearchHit])


@pytest.mark.unit
@pytest.mark.parametrize(
    "params",
    [
        {},
        {"q": ""},
        {"q": "   "},
        {"q": "x" * 201},
        {"q": "a\x00b"},
        {"q": "doe", "clients_limit": -1},
        {"q": "doe", "clients_limit": 51},
        {"q": "doe", "documents_limit": 21},
    ],
)
async def test_search_rejects_invalid_parameters(
    client: AsyncClient, params: dict[str, str | int]
) -> None:
    response = await client.get("/search", params=params)

    assert response.status_code == 422


@pytest.mark.unit
async def test_search_returns_503_when_search_is_unreachable(
    client: AsyncClient, fake_search_client: NonCallableMagicMock
) -> None:
    fake_search_client.multi_search.side_effect = MeilisearchCommunicationError("refused")

    response = await client.get("/search", params={"q": "doe"})

    assert response.status_code == 503
    assert response.json() == {"detail": "Search unavailable"}


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("wealth", '"wealth"'),
        ('say "hi"', '"say \\"hi\\""'),
        ("back\\slash", '"back\\\\slash"'),
        ("Zoë", '"Zoë"'),
    ],
)
def test_filter_string_quotes_and_escapes(value: str, expected: str) -> None:
    assert filter_string(value) == expected


@pytest.mark.unit
def test_merge_ids_keeps_first_occurrence_order_and_limit() -> None:
    assert merge_ids([["a", "b"], ["b", "c", "d"]], limit=3) == ["a", "b", "c"]


@pytest.fixture
async def db_pool(db_settings: Settings) -> AsyncGenerator[DbPool]:
    pool = await create_pool(db_settings)
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture
async def search_client(db_settings: Settings) -> AsyncGenerator[SearchClient]:
    async with create_search_client(db_settings) as client:
        yield client


@pytest.fixture
async def ids(
    db_client: AsyncClient, db_pool: DbPool, search_client: SearchClient
) -> dict[str, str]:
    """Clients and documents created through the API and indexed: name or title -> id."""
    created: dict[str, str] = {}
    for name, payload in (("john", JOHN), ("zoe", ZOE), ("anna", ANNA)):
        created[name] = (await db_client.post("/clients", json=payload)).json()["id"]
    for title, content in DOCUMENTS:
        response = await db_client.post(
            f"/clients/{created['john']}/documents", json={"title": title, "content": content}
        )
        created[title] = response.json()["id"]
    outbox_worker = create_outbox_worker(db_pool, search_client)
    while await outbox_worker.run_once():
        pass
    return created


async def search(db_client: AsyncClient, q: str, **params: int) -> list[SearchHit]:
    response = await db_client.get("/search", params={"q": q, **params})
    assert response.status_code == 200
    return HITS.validate_python(response.json())


def client_ids(hits: list[SearchHit]) -> list[str]:
    return [str(hit.client.id) for hit in hits if isinstance(hit, ClientHit)]


def document_hits(hits: list[SearchHit]) -> list[DocumentHit]:
    return [hit for hit in hits if isinstance(hit, DocumentHit)]


def document_ids(hits: list[SearchHit]) -> list[str]:
    return [str(hit.document.id) for hit in document_hits(hits)]


@pytest.mark.integration
@pytest.mark.parametrize("q", ["NevisWealth", "neviswealth", "NEVISWEALTH", "john.doe"])
async def test_search_finds_client_by_email(
    db_client: AsyncClient, ids: dict[str, str], q: str
) -> None:
    assert client_ids(await search(db_client, q))[0] == ids["john"]


@pytest.mark.integration
async def test_search_ranks_keyword_matches_before_substring_matches(
    db_client: AsyncClient, ids: dict[str, str]
) -> None:
    # Anna has the word "capital"; Zoë only has it inside "AcmeCapital".
    assert client_ids(await search(db_client, "capital")) == [ids["anna"], ids["zoe"]]


@pytest.mark.integration
@pytest.mark.parametrize(
    ("q", "who"),
    [
        ("allister", "zoe"),
        ("ALLISTER", "zoe"),
        ("mCaLLiStEr", "zoe"),
        ("acmecap", "zoe"),
        ("zoe", "zoe"),
        ("ZOË", "zoe"),
        ("family", "anna"),
        ("sustainable", "john"),
        ("Doe", "john"),
    ],
)
async def test_search_matches_substrings_regardless_of_case_and_accents(
    db_client: AsyncClient, ids: dict[str, str], q: str, who: str
) -> None:
    assert client_ids(await search(db_client, q)) == [ids[who]]


@pytest.mark.integration
async def test_search_finds_both_substring_matches(
    db_client: AsyncClient, ids: dict[str, str]
) -> None:
    # "wealth" is only ever inside a longer word ("neviswealth"), for John and for Anna.
    assert set(client_ids(await search(db_client, "wealth"))) == {ids["john"], ids["anna"]}


@pytest.mark.integration
async def test_search_finds_documents_by_meaning(
    db_client: AsyncClient, ids: dict[str, str]
) -> None:
    hits = await search(db_client, "address proof")

    assert ids["Utility bill March"] in document_ids(hits)


@pytest.mark.integration
async def test_document_results_carry_the_document_and_the_matched_text(
    db_client: AsyncClient, ids: dict[str, str]
) -> None:
    first = document_hits(await search(db_client, "Vattenfall"))[0]

    assert str(first.document.id) == ids["Utility bill March"]
    assert "Vattenfall" in first.matched_text


@pytest.mark.integration
async def test_search_lists_clients_before_documents_and_respects_limits(
    db_client: AsyncClient, ids: dict[str, str]
) -> None:
    hits = await search(db_client, "NevisWealth", clients_limit=1, documents_limit=2)

    assert [hit.type for hit in hits] == ["client", "document", "document"]


@pytest.mark.integration
async def test_search_lists_a_long_document_once(
    db_client: AsyncClient, db_pool: DbPool, search_client: SearchClient, ids: dict[str, str]
) -> None:
    content = "Vattenfall electricity bill. " * 200  # several chunks
    response = await db_client.post(
        f"/clients/{ids['john']}/documents", json={"title": "Long bill", "content": content}
    )
    await create_outbox_worker(db_pool, search_client).run_once()

    found = document_ids(await search(db_client, "Vattenfall", documents_limit=20))

    assert found.count(response.json()["id"]) == 1


@pytest.mark.integration
@pytest.mark.parametrize("q", ['x" OR id EXISTS OR email CONTAINS "y', "back\\slash", "\"'`;--"])
async def test_search_treats_hostile_input_as_text(
    db_client: AsyncClient, ids: dict[str, str], q: str
) -> None:
    assert client_ids(await search(db_client, q)) == []


@pytest.mark.integration
async def test_search_with_nothing_indexed_returns_empty_list(db_client: AsyncClient) -> None:
    assert await search(db_client, "anything") == []

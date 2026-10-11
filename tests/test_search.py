from unittest.mock import NonCallableMagicMock

import pytest
from httpx2 import AsyncClient
from pydantic import TypeAdapter

from nevis.search.schemas import ClientHit, DocumentHit, SearchHit

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
async def test_search_without_anything_to_look_for_does_not_touch_the_database(
    client: AsyncClient, fake_pool: NonCallableMagicMock, fake_embedder: NonCallableMagicMock
) -> None:
    # "--" has no word to match clients with, and documents_limit=0 asks for no documents.
    response = await client.get("/search", params={"q": "--", "documents_limit": 0})

    assert response.json() == []
    assert fake_pool.mock_calls == []
    fake_embedder.embed_query.assert_not_called()


@pytest.fixture
async def ids(db_client: AsyncClient) -> dict[str, str]:
    """Clients and documents created through the API, searchable at once: name or title -> id."""
    created: dict[str, str] = {}
    for name, payload in (("john", JOHN), ("zoe", ZOE), ("anna", ANNA)):
        created[name] = (await db_client.post("/clients", json=payload)).json()["id"]
    for title, content in DOCUMENTS:
        response = await db_client.post(
            f"/clients/{created['john']}/documents", json={"title": title, "content": content}
        )
        created[title] = response.json()["id"]
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
async def test_search_ranks_whole_word_matches_before_part_of_a_word_matches(
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
    db_client: AsyncClient, ids: dict[str, str]
) -> None:
    content = "Vattenfall electricity bill. " * 200  # several chunks
    response = await db_client.post(
        f"/clients/{ids['john']}/documents", json={"title": "Long bill", "content": content}
    )

    found = document_ids(await search(db_client, "Vattenfall", documents_limit=20))

    assert found.count(response.json()["id"]) == 1


@pytest.mark.integration
@pytest.mark.parametrize("q", ['x" OR id EXISTS OR email CONTAINS "y', "back\\slash", "\"'`;--"])
async def test_search_treats_hostile_input_as_text(
    db_client: AsyncClient, ids: dict[str, str], q: str
) -> None:
    assert client_ids(await search(db_client, q)) == []


@pytest.mark.unit
async def test_search_returns_503_when_the_database_is_unreachable(
    client: AsyncClient, fake_pool: NonCallableMagicMock
) -> None:
    fake_pool.fetch.side_effect = OSError("refused")

    response = await client.get("/search", params={"q": "doe"})

    assert response.status_code == 503
    assert response.json() == {"detail": "Database unavailable"}


@pytest.mark.integration
async def test_new_client_and_document_are_searchable_right_after_creation(
    db_client: AsyncClient,
) -> None:
    created = (
        await db_client.post(
            "/clients",
            json={"first_name": "Greta", "last_name": "Lindqvist", "email": "greta@fjord.no"},
        )
    ).json()
    document = (
        await db_client.post(
            f"/clients/{created['id']}/documents",
            json={
                "title": "Mortgage offer",
                "content": "Fixed rate mortgage offer from Fjordbank.",
            },
        )
    ).json()

    hits = await search(db_client, "Lindqvist")

    assert client_ids(hits) == [created["id"]]
    assert document["id"] in document_ids(await search(db_client, "Fjordbank"))


@pytest.mark.integration
@pytest.mark.parametrize(
    ("q", "who"),
    [("neviswelth", "company"), ("jon doe", "john"), ("Zoe Mcalister", "zoe")],
)
async def test_search_tolerates_typos_in_client_names(
    db_client: AsyncClient, ids: dict[str, str], q: str, who: str
) -> None:
    company = (
        await db_client.post(
            "/clients",
            json={"first_name": "NevisWealth", "last_name": "Ltd", "email": "office@nw.example"},
        )
    ).json()["id"]

    # John's email also contains "neviswealth", so the company only has to be among the matches.
    assert {**ids, "company": company}[who] in client_ids(await search(db_client, q))


@pytest.mark.integration
async def test_search_lists_typo_matches_after_part_of_a_word_matches_without_repeating_them(
    db_client: AsyncClient, ids: dict[str, str]
) -> None:
    fuzzy = (
        await db_client.post(
            "/clients",
            json={"first_name": "Jon", "last_name": "Doe", "email": "jd@elsewhere.example"},
        )
    ).json()["id"]

    # John's text contains "john doe" as whole words; Jon Doe only resembles it.
    assert client_ids(await search(db_client, "john doe")) == [ids["john"], fuzzy]


@pytest.mark.integration
@pytest.mark.parametrize("q", ["jose", "JOSÉ", "José"])
async def test_search_ignores_accents_and_case_in_client_names(
    db_client: AsyncClient, q: str
) -> None:
    created = (
        await db_client.post(
            "/clients",
            json={"first_name": "José", "last_name": "Alvarez", "email": "j.alvarez@mail.example"},
        )
    ).json()["id"]

    assert client_ids(await search(db_client, q)) == [created]


@pytest.mark.integration
@pytest.mark.parametrize("q", ["xqzv", "chocolate cake recipe", "--", "!!!"])
async def test_search_returns_no_clients_for_junk(
    db_client: AsyncClient, ids: dict[str, str], q: str
) -> None:
    assert client_ids(await search(db_client, q)) == []


@pytest.mark.integration
async def test_search_matches_short_queries_only_as_whole_words(
    db_client: AsyncClient, ids: dict[str, str]
) -> None:
    # "uk" is a word of Zoë's email domain; "do" is only the start of "doe".
    assert client_ids(await search(db_client, "uk")) == [ids["zoe"]]
    assert client_ids(await search(db_client, "do")) == []


@pytest.mark.integration
@pytest.mark.parametrize(
    ("q", "text", "decoy"),
    [
        ("x_z", "code x_z here", "code xyz here"),
        ("a%b", "rate a%b here", "rate axxb here"),
        ("abc\\", "path abc\\", "path xyz"),
        ("a.b", "ratio a.b here", "ratio axb here"),  # 3+ characters: LIKE and the regex
        ("a.", "grade a. here", "grade ax here"),  # under 3 characters: the regex alone
    ],
)
async def test_search_treats_like_metacharacters_as_literal_text(
    db_client: AsyncClient, q: str, text: str, decoy: str
) -> None:
    ids = []
    for index, description in enumerate((text, decoy)):
        payload = {
            "first_name": f"Person{index}",
            "last_name": "Test",
            "email": f"p{index}@mail.example",
            "description": description,
        }
        ids.append((await db_client.post("/clients", json=payload)).json()["id"])

    assert client_ids(await search(db_client, q)) == [ids[0]]


@pytest.mark.integration
@pytest.mark.parametrize(
    "q",
    [
        '"',
        '"unclosed phrase',
        "!",
        "!!a",
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
        "- -",
        "a -b",
        "x" * 200,
        "<->",
        "'; DROP TABLE documents; --",
        "😀 emoji",
        "or",
        "OR OR",
    ],
)
async def test_search_survives_arbitrary_input(
    db_client: AsyncClient, ids: dict[str, str], q: str
) -> None:
    response = await db_client.get("/search", params={"q": q})

    assert response.status_code == 200


@pytest.mark.integration
async def test_search_respects_the_clients_limit(
    db_client: AsyncClient, ids: dict[str, str]
) -> None:
    assert len(client_ids(await search(db_client, "wealth", clients_limit=1))) == 1
    assert client_ids(await search(db_client, "wealth", clients_limit=0)) == []


@pytest.mark.integration
async def test_search_respects_the_documents_limit(
    db_client: AsyncClient, ids: dict[str, str]
) -> None:
    assert len(document_ids(await search(db_client, "bill", documents_limit=2))) == 2
    assert document_ids(await search(db_client, "bill", documents_limit=0)) == []


@pytest.mark.integration
async def test_search_with_nothing_indexed_returns_empty_list(db_client: AsyncClient) -> None:
    assert await search(db_client, "anything") == []

import uuid
from unittest.mock import NonCallableMagicMock

import pytest
from httpx2 import AsyncClient

VALID_CLIENT: dict[str, object] = {
    "first_name": "John",
    "last_name": "Doe",
    "email": "john.doe@neviswealth.com",
}


@pytest.mark.unit
@pytest.mark.parametrize("missing", ["first_name", "last_name", "email"])
async def test_create_client_rejects_missing_required_field(
    client: AsyncClient, missing: str
) -> None:
    payload = {key: value for key, value in VALID_CLIENT.items() if key != missing}

    response = await client.post("/clients", json=payload)

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", missing]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("email", "not-an-email"),
        ("email", "john@"),
        ("first_name", "   "),
        ("last_name", ""),
        ("first_name", "x" * 101),
        ("social_links", ["https://example.com"] * 21),
        ("social_links", [""]),
        ("social_links", "https://example.com"),
        ("description", "x" * 10_001),
        # Postgres text columns can't store NUL; reject it here instead of failing on INSERT.
        ("first_name", "Jo\x00hn"),
        ("last_name", "Doe\x00"),
        ("email", "john\x00@neviswealth.com"),
        ("description", "a\x00b"),
        ("social_links", ["https://example.com/\x00"]),
        # Social links are rendered as hrefs, so only absolute http(s) URLs are accepted.
        ("social_links", ["javascript:alert(1)"]),
        ("social_links", ["data:text/html,<script>alert(1)</script>"]),
        ("social_links", ["ftp://example.com/file"]),
        ("social_links", ["linkedin.com/in/johndoe"]),
        ("social_links", ["not a url"]),
        ("social_links", ["https://"]),
        ("social_links", ["https://exa mple.com"]),
        ("social_links", ["https://[::1"]),
    ],
)
async def test_create_client_rejects_invalid_field(
    client: AsyncClient, field: str, value: object
) -> None:
    response = await client.post("/clients", json={**VALID_CLIENT, field: value})

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"][:2] == ["body", field]


@pytest.mark.unit
async def test_create_client_rejects_unknown_field(client: AsyncClient) -> None:
    response = await client.post("/clients", json={**VALID_CLIENT, "is_admin": True})

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "is_admin"]


@pytest.mark.unit
async def test_create_client_returns_503_when_database_is_unreachable(
    client: AsyncClient, fake_pool: NonCallableMagicMock
) -> None:
    fake_pool.fetchrow.side_effect = ConnectionRefusedError

    response = await client.post("/clients", json=VALID_CLIENT)

    assert response.status_code == 503
    assert response.json() == {"detail": "Database unavailable"}


@pytest.mark.integration
async def test_create_client_returns_201_with_defaults(db_client: AsyncClient) -> None:
    response = await db_client.post("/clients", json=VALID_CLIENT)

    assert response.status_code == 201
    body = response.json()
    uuid.UUID(body.pop("id"))
    assert body == {**VALID_CLIENT, "description": None, "social_links": []}


@pytest.mark.integration
async def test_create_client_stores_optional_fields_and_trims_whitespace(
    db_client: AsyncClient,
) -> None:
    payload = {
        **VALID_CLIENT,
        "first_name": "  John ",
        "description": "Prefers sustainable funds",
        "social_links": ["https://linkedin.com/in/johndoe"],
    }

    response = await db_client.post("/clients", json=payload)

    assert response.status_code == 201
    body = response.json()
    assert body["first_name"] == "John"
    assert body["description"] == "Prefers sustainable funds"
    assert body["social_links"] == ["https://linkedin.com/in/johndoe"]


@pytest.mark.integration
async def test_create_client_assigns_distinct_ids(db_client: AsyncClient) -> None:
    first = (await db_client.post("/clients", json=VALID_CLIENT)).json()
    second = (
        await db_client.post("/clients", json={**VALID_CLIENT, "email": "jane.doe@neviswealth.com"})
    ).json()

    assert first["id"] != second["id"]


@pytest.mark.integration
async def test_create_client_rejects_duplicate_email_case_insensitively(
    db_client: AsyncClient,
) -> None:
    assert (await db_client.post("/clients", json=VALID_CLIENT)).status_code == 201

    response = await db_client.post(
        "/clients", json={**VALID_CLIENT, "email": "John.Doe@NevisWealth.com"}
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "A client with this email already exists"}

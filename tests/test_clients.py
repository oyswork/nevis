import uuid

import pytest
from fastapi.testclient import TestClient

VALID_CLIENT: dict[str, object] = {
    "first_name": "John",
    "last_name": "Doe",
    "email": "john.doe@neviswealth.com",
}


@pytest.mark.unit
@pytest.mark.parametrize("missing", ["first_name", "last_name", "email"])
def test_create_client_rejects_missing_required_field(client: TestClient, missing: str) -> None:
    payload = {key: value for key, value in VALID_CLIENT.items() if key != missing}

    response = client.post("/clients", json=payload)

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
    ],
)
def test_create_client_rejects_invalid_field(client: TestClient, field: str, value: object) -> None:
    response = client.post("/clients", json={**VALID_CLIENT, field: value})

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"][:2] == ["body", field]


@pytest.mark.unit
def test_create_client_rejects_unknown_field(client: TestClient) -> None:
    response = client.post("/clients", json={**VALID_CLIENT, "is_admin": True})

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "is_admin"]


@pytest.mark.integration
def test_create_client_returns_201_with_defaults(db_client: TestClient) -> None:
    response = db_client.post("/clients", json=VALID_CLIENT)

    assert response.status_code == 201
    body = response.json()
    uuid.UUID(body.pop("id"))
    assert body == {**VALID_CLIENT, "description": None, "social_links": []}


@pytest.mark.integration
def test_create_client_stores_optional_fields_and_trims_whitespace(db_client: TestClient) -> None:
    payload = {
        **VALID_CLIENT,
        "first_name": "  John ",
        "description": "Prefers sustainable funds",
        "social_links": ["https://linkedin.com/in/johndoe"],
    }

    response = db_client.post("/clients", json=payload)

    assert response.status_code == 201
    body = response.json()
    assert body["first_name"] == "John"
    assert body["description"] == "Prefers sustainable funds"
    assert body["social_links"] == ["https://linkedin.com/in/johndoe"]


@pytest.mark.integration
def test_create_client_assigns_distinct_ids(db_client: TestClient) -> None:
    first = db_client.post("/clients", json=VALID_CLIENT).json()
    second = db_client.post(
        "/clients", json={**VALID_CLIENT, "email": "jane.doe@neviswealth.com"}
    ).json()

    assert first["id"] != second["id"]


@pytest.mark.integration
def test_create_client_rejects_duplicate_email_case_insensitively(db_client: TestClient) -> None:
    assert db_client.post("/clients", json=VALID_CLIENT).status_code == 201

    response = db_client.post(
        "/clients", json={**VALID_CLIENT, "email": "John.Doe@NevisWealth.com"}
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "A client with this email already exists"}

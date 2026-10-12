"""The OpenAPI document keeps the contract the assignment specifies (paths, fields, formats)."""

import tomllib
from pathlib import Path
from typing import Any

import pytest

from nevis.config import Settings
from nevis.main import create_app

# Any: the OpenAPI document is free-form JSON.
type Schema = dict[str, Any]


@pytest.fixture
def openapi(settings: Settings) -> Schema:
    # Generating the document runs no lifespan: no database or models.
    document: Schema = create_app(settings).openapi()
    return document


def component(openapi: Schema, operation: Schema, status: str) -> Schema:
    reference = operation["responses"][status]["content"]["application/json"]["schema"]["$ref"]
    schema: Schema = openapi["components"]["schemas"][reference.rsplit("/", 1)[-1]]
    return schema


@pytest.mark.unit
def test_the_spec_paths_and_methods_exist(openapi: Schema) -> None:
    paths = openapi["paths"]

    assert "post" in paths["/clients"]
    assert "post" in paths["/clients/{client_id}/documents"]
    assert "get" in paths["/search"]


@pytest.mark.unit
def test_creating_a_client_takes_and_returns_the_spec_fields(openapi: Schema) -> None:
    operation = openapi["paths"]["/clients"]["post"]
    request = openapi["components"]["schemas"]["ClientCreate"]

    client = component(openapi, operation, "201")

    assert set(request["required"]) == {"first_name", "last_name", "email"}
    assert request["properties"]["email"]["format"] == "email"
    assert {"id", "first_name", "last_name", "email", "description", "social_links"} <= set(
        client["properties"]
    )
    assert client["properties"]["email"]["format"] == "email"


@pytest.mark.unit
def test_creating_a_document_returns_the_spec_fields(openapi: Schema) -> None:
    operation = openapi["paths"]["/clients/{client_id}/documents"]["post"]

    document = component(openapi, operation, "201")

    assert {"id", "client_id", "title", "content", "created_at"} <= set(document["properties"])
    assert document["properties"]["created_at"]["format"] == "date-time"


@pytest.mark.unit
def test_search_takes_a_required_q_and_returns_an_array(openapi: Schema) -> None:
    operation = openapi["paths"]["/search"]["get"]
    parameters = {parameter["name"]: parameter for parameter in operation["parameters"]}

    assert parameters["q"]["required"] is True
    response = operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert response["type"] == "array"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("path", "method", "status"),
    [
        ("/clients", "post", "409"),
        ("/clients/{client_id}", "get", "404"),
        ("/clients/{client_id}/documents", "post", "404"),
        ("/documents/{document_id}", "get", "404"),
        ("/search", "get", "503"),
        ("/clients/{client_id}/documents", "post", "503"),
    ],
)
def test_error_responses_document_their_body(
    openapi: Schema, path: str, method: str, status: str
) -> None:
    error = component(openapi, openapi["paths"][path][method], status)

    assert error["properties"]["detail"]["type"] == "string"


@pytest.mark.unit
def test_document_upload_documents_retry_after_on_503(openapi: Schema) -> None:
    response = openapi["paths"]["/clients/{client_id}/documents"]["post"]["responses"]["503"]

    assert response["headers"]["Retry-After"]["schema"]["type"] == "integer"


@pytest.mark.unit
def test_the_documented_api_version_is_the_project_version(openapi: Schema) -> None:
    pyproject = Path(__file__).parents[1] / "pyproject.toml"
    project_version = tomllib.loads(pyproject.read_text())["project"]["version"]

    assert openapi["info"]["version"] == project_version

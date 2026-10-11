"""Error responses shared by the routers: their texts, body model and OpenAPI entries."""

from typing import Any

from pydantic import BaseModel

CLIENT_NOT_FOUND_DETAIL = "Client not found"
DOCUMENT_NOT_FOUND_DETAIL = "Document not found"
DUPLICATE_EMAIL_DETAIL = "A client with this email already exists"


class ErrorResponse(BaseModel):
    """The body of every error except 422, whose `detail` lists the invalid fields instead."""

    detail: str


def error_response(description: str) -> dict[str, Any]:
    """An OpenAPI `responses` entry for an error with an ErrorResponse body.

    Any: matches the type of FastAPI's `responses` parameter (free-form OpenAPI objects).
    """
    return {"model": ErrorResponse, "description": description}

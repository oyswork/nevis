import logging
import uuid

from fastapi import APIRouter, HTTPException, status

from nevis.api.errors import (
    CLIENT_NOT_FOUND_DETAIL,
    DOCUMENT_NOT_FOUND_DETAIL,
    EMBEDDING_QUEUE_FULL_DETAIL,
    error_response,
)
from nevis.db import DATABASE_UNAVAILABLE_DESCRIPTION, DATABASE_UNAVAILABLE_RESPONSES, PoolDep
from nevis.documents import repository
from nevis.documents.schemas import DocumentCreate, DocumentRead
from nevis.embeddings import QUEUE_FULL_RETRY_AFTER_SECONDS, EmbedderDep, EmbeddingQueueFullError

logger = logging.getLogger(__name__)

router = APIRouter(tags=["documents"])


@router.post(
    "/clients/{client_id}/documents",
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_404_NOT_FOUND: error_response(CLIENT_NOT_FOUND_DETAIL),
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            **error_response(
                f"{DATABASE_UNAVAILABLE_DESCRIPTION}, or {EMBEDDING_QUEUE_FULL_DETAIL.lower()}"
                " (then with Retry-After)"
            ),
            "headers": {
                "Retry-After": {
                    "description": "Seconds to wait before retrying",
                    "schema": {"type": "integer"},
                }
            },
        },
    },
)
async def create_document(
    client_id: uuid.UUID,
    payload: DocumentCreate,
    pool: PoolDep,
    embedder: EmbedderDep,
) -> DocumentRead:
    """Store a document for the client.

    It is searchable as soon as this returns. If the worker has a local summarization model,
    `summary` is filled in later (read it back with GET /documents/{id}); otherwise it stays null.
    When too many documents are already waiting to be processed, nothing is stored and the
    response is 503 with Retry-After.
    """
    try:
        document = await repository.create_document(pool, embedder, client_id, payload)
    except repository.ClientNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, CLIENT_NOT_FOUND_DETAIL) from exc
    except EmbeddingQueueFullError as exc:
        logger.warning("Embedding queue full: refused a document for client %s", client_id)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            EMBEDDING_QUEUE_FULL_DETAIL,
            headers={"Retry-After": str(QUEUE_FULL_RETRY_AFTER_SECONDS)},
        ) from exc
    return DocumentRead.model_validate(document)


@router.get(
    "/documents/{document_id}",
    responses={
        status.HTTP_404_NOT_FOUND: error_response(DOCUMENT_NOT_FOUND_DETAIL),
        **DATABASE_UNAVAILABLE_RESPONSES,
    },
)
async def get_document(document_id: uuid.UUID, pool: PoolDep) -> DocumentRead:
    document = await repository.get_document(pool, document_id)
    if document is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, DOCUMENT_NOT_FOUND_DETAIL)
    return DocumentRead.model_validate(document)

import uuid

from fastapi import APIRouter, HTTPException, status

from nevis.clients.router import CLIENT_NOT_FOUND_DETAIL
from nevis.db import DATABASE_UNAVAILABLE_RESPONSES, PoolDep
from nevis.documents import repository
from nevis.documents.schemas import DocumentCreate, DocumentRead

router = APIRouter(tags=["documents"])

DOCUMENT_NOT_FOUND_DETAIL = "Document not found"


@router.post(
    "/clients/{client_id}/documents",
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_404_NOT_FOUND: {"description": CLIENT_NOT_FOUND_DETAIL},
        **DATABASE_UNAVAILABLE_RESPONSES,
    },
)
async def create_document(
    client_id: uuid.UUID, payload: DocumentCreate, pool: PoolDep
) -> DocumentRead:
    """Store a document for the client. It becomes searchable shortly after (asynchronously)."""
    try:
        document = await repository.create_document(pool, client_id, payload)
    except repository.ClientNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, CLIENT_NOT_FOUND_DETAIL) from exc
    return DocumentRead.model_validate(document)


@router.get(
    "/documents/{document_id}",
    responses={
        status.HTTP_404_NOT_FOUND: {"description": DOCUMENT_NOT_FOUND_DETAIL},
        **DATABASE_UNAVAILABLE_RESPONSES,
    },
)
async def get_document(document_id: uuid.UUID, pool: PoolDep) -> DocumentRead:
    document = await repository.get_document(pool, document_id)
    if document is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, DOCUMENT_NOT_FOUND_DETAIL)
    return DocumentRead.model_validate(document)

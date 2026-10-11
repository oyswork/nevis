import uuid

from fastapi import APIRouter, HTTPException, status

from nevis.api.errors import CLIENT_NOT_FOUND_DETAIL, DUPLICATE_EMAIL_DETAIL, error_response
from nevis.clients import repository
from nevis.clients.schemas import ClientCreate, ClientRead
from nevis.db import DATABASE_UNAVAILABLE_RESPONSES, PoolDep

router = APIRouter(prefix="/clients", tags=["clients"])


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_409_CONFLICT: error_response(DUPLICATE_EMAIL_DETAIL),
        **DATABASE_UNAVAILABLE_RESPONSES,
    },
)
async def create_client(payload: ClientCreate, pool: PoolDep) -> ClientRead:
    try:
        client = await repository.create_client(pool, payload)
    except repository.EmailAlreadyExistsError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, DUPLICATE_EMAIL_DETAIL) from exc
    return ClientRead.model_validate(client)


@router.get(
    "/{client_id}",
    responses={
        status.HTTP_404_NOT_FOUND: error_response(CLIENT_NOT_FOUND_DETAIL),
        **DATABASE_UNAVAILABLE_RESPONSES,
    },
)
async def get_client(client_id: uuid.UUID, pool: PoolDep) -> ClientRead:
    client = await repository.get_client(pool, client_id)
    if client is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, CLIENT_NOT_FOUND_DETAIL)
    return ClientRead.model_validate(client)

from fastapi import APIRouter, HTTPException, status

from nevis.clients import repository
from nevis.clients.schemas import ClientCreate, ClientRead
from nevis.db import PoolDep

router = APIRouter(prefix="/clients", tags=["clients"])

DUPLICATE_EMAIL_DETAIL = "A client with this email already exists"


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    responses={status.HTTP_409_CONFLICT: {"description": DUPLICATE_EMAIL_DETAIL}},
)
async def create_client(payload: ClientCreate, pool: PoolDep) -> ClientRead:
    try:
        client = await repository.create_client(pool, payload)
    except repository.EmailAlreadyExistsError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, DUPLICATE_EMAIL_DETAIL) from exc
    return ClientRead.model_validate(client, from_attributes=True)

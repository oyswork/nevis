import uuid

import asyncpg

from nevis.clients.schemas import ClientCreate
from nevis.db import DbPool
from nevis.db_models import Client
from nevis.outbox import repository as outbox
from nevis.outbox.repository import OutboxKind
from nevis.text import client_search_text

EMAIL_UNIQUE_INDEX = "uq_clients_email_lower"

INSERT_CLIENT = """
    INSERT INTO clients (first_name, last_name, email, description, social_links, search_text)
    VALUES ($1, $2, $3, $4, $5, $6)
    RETURNING *
"""

SELECT_CLIENT = "SELECT * FROM clients WHERE id = $1"


class EmailAlreadyExistsError(Exception):
    """Another client already uses this email (compared case-insensitively)."""


async def create_client(pool: DbPool, data: ClientCreate) -> Client:
    """Insert the client and, in the same transaction, the job that indexes it for search."""
    try:
        async with pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                INSERT_CLIENT,
                data.first_name,
                data.last_name,
                data.email,
                data.description,
                data.social_links,
                client_search_text(
                    first_name=data.first_name,
                    last_name=data.last_name,
                    email=data.email,
                    description=data.description,
                ),
            )
            if row is None:  # INSERT ... RETURNING always yields the inserted row
                msg = "INSERT INTO clients returned no row"
                raise RuntimeError(msg)
            client = Client(**row)
            await outbox.enqueue(connection, OutboxKind.INDEX_CLIENT, client.id)
    except asyncpg.UniqueViolationError as exc:
        if exc.constraint_name == EMAIL_UNIQUE_INDEX:
            raise EmailAlreadyExistsError from exc
        raise
    return client


async def get_client(pool: DbPool, client_id: uuid.UUID) -> Client | None:
    row = await pool.fetchrow(SELECT_CLIENT, client_id)
    return None if row is None else Client(**row)

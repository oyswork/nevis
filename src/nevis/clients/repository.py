import asyncpg

from nevis.clients.schemas import ClientCreate
from nevis.db import DbPool
from nevis.db_models import Client

EMAIL_UNIQUE_INDEX = "uq_clients_email_lower"

INSERT_CLIENT = """
    INSERT INTO clients (first_name, last_name, email, description, social_links)
    VALUES ($1, $2, $3, $4, $5)
    RETURNING *
"""


class EmailAlreadyExistsError(Exception):
    """Another client already uses this email (compared case-insensitively)."""


async def create_client(pool: DbPool, data: ClientCreate) -> Client:
    try:
        row = await pool.fetchrow(
            INSERT_CLIENT,
            data.first_name,
            data.last_name,
            data.email,
            data.description,
            data.social_links,
        )
    except asyncpg.UniqueViolationError as exc:
        if exc.constraint_name == EMAIL_UNIQUE_INDEX:
            raise EmailAlreadyExistsError from exc
        raise
    if row is None:  # INSERT ... RETURNING always yields the inserted row
        msg = "INSERT INTO clients returned no row"
        raise RuntimeError(msg)
    return Client(**row)

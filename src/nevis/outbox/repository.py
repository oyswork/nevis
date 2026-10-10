import datetime
import uuid
from enum import StrEnum

from nevis.db import DbConnection, DbPool
from nevis.db_models import Outbox


class OutboxKind(StrEnum):
    """Job kinds; outbox_kind_check in schema/schema.sql lists the same values."""

    INDEX_CLIENT = "index_client"


INSERT_JOB = "INSERT INTO outbox (kind, entity_id) VALUES ($1, $2)"

# Takes a lease instead of holding row locks: the claim commits at once, so no transaction stays
# open while a job runs, and a crashed worker's jobs become claimable when the lease expires.
# SKIP LOCKED keeps concurrent workers from claiming the same job.
CLAIM_JOBS = """
    UPDATE outbox
    SET locked_until = now() + $2::interval, attempts = attempts + 1
    WHERE id IN (
        SELECT id FROM outbox
        WHERE failed_at IS NULL
          AND available_at <= now()
          AND (locked_until IS NULL OR locked_until <= now())
        ORDER BY available_at, id
        LIMIT $1
        FOR UPDATE SKIP LOCKED
    )
    RETURNING *
"""

DELETE_JOB = "DELETE FROM outbox WHERE id = $1"

# Releases the lease and schedules the next attempt, or gives up once attempts reach $4.
RETRY_JOB = """
    UPDATE outbox
    SET locked_until = NULL,
        last_error = $2,
        available_at = now() + $3::interval,
        failed_at = CASE WHEN attempts >= $4 THEN now() END
    WHERE id = $1
    RETURNING failed_at IS NOT NULL
"""


async def enqueue(connection: DbConnection, kind: OutboxKind, entity_id: uuid.UUID) -> None:
    """Add a job in the caller's transaction, so it exists exactly when the change commits."""
    await connection.execute(INSERT_JOB, kind, entity_id)


async def claim_jobs(pool: DbPool, limit: int, lease: datetime.timedelta) -> list[Outbox]:
    return [Outbox(**row) for row in await pool.fetch(CLAIM_JOBS, limit, lease)]


async def complete_job(pool: DbPool, job_id: int) -> None:
    await pool.execute(DELETE_JOB, job_id)


async def retry_job(
    pool: DbPool, job_id: int, error: str, delay: datetime.timedelta, max_attempts: int
) -> bool:
    """Schedule another attempt; returns True if the job has used up its attempts instead."""
    gave_up: bool = await pool.fetchval(RETRY_JOB, job_id, error, delay, max_attempts)
    return gave_up

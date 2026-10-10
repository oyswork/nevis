import asyncio
import contextlib
import datetime
import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping

from nevis.db import DbPool
from nevis.db_models import Outbox
from nevis.outbox import repository
from nevis.outbox.repository import OutboxKind

logger = logging.getLogger(__name__)

type JobHandler = Callable[[uuid.UUID], Awaitable[None]]

BATCH_SIZE = 20
POLL_INTERVAL_SECONDS = 0.5
# After the worker itself fails (e.g. the database is down), not a job.
ERROR_PAUSE_SECONDS = 5
# Longer than any job takes. A job that outlives it can run twice, which handlers tolerate.
LEASE = datetime.timedelta(minutes=2)
MAX_ATTEMPTS = 8
RETRY_BASE_DELAY = datetime.timedelta(seconds=1)
RETRY_MAX_DELAY = datetime.timedelta(minutes=10)
LAST_ERROR_MAX_LENGTH = 2000
# Doubling stops here, long after RETRY_MAX_DELAY is reached, so the delay can't overflow.
RETRY_MAX_DOUBLINGS = 20


def retry_delay(attempts: int) -> datetime.timedelta:
    """Exponential backoff after the given number of failed attempts."""
    doublings = min(max(attempts - 1, 0), RETRY_MAX_DOUBLINGS)
    return min(RETRY_BASE_DELAY * (1 << doublings), RETRY_MAX_DELAY)


class OutboxWorker:
    """Claims due jobs and runs the handler for their kind.

    Handlers must be idempotent: a job runs at least once, more often if its lease expires.
    """

    def __init__(self, pool: DbPool, handlers: Mapping[OutboxKind, JobHandler]) -> None:
        self._pool = pool
        self._handlers = handlers
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self.run_forever(), name="outbox-worker")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def run_forever(self) -> None:
        while True:
            try:
                processed = await self.run_once()
            except Exception:
                logger.exception("Outbox worker iteration failed; retrying shortly")
                await asyncio.sleep(ERROR_PAUSE_SECONDS)
                continue
            if processed < BATCH_SIZE:  # a full batch means more may be waiting
                await asyncio.sleep(POLL_INTERVAL_SECONDS)

    async def run_once(self) -> int:
        """Run one batch of due jobs; returns how many were claimed."""
        jobs = await repository.claim_jobs(self._pool, BATCH_SIZE, LEASE)
        for job in jobs:
            await self._run(job)
        return len(jobs)

    async def _run(self, job: Outbox) -> None:
        try:
            await self._handlers[OutboxKind(job.kind)](job.entity_id)
        except Exception as exc:
            await self._retry(job, exc)
            return
        await repository.complete_job(self._pool, job.id)

    async def _retry(self, job: Outbox, exc: Exception) -> None:
        error = f"{type(exc).__name__}: {exc}"[:LAST_ERROR_MAX_LENGTH]
        delay = retry_delay(job.attempts)
        gave_up = await repository.retry_job(self._pool, job.id, error, delay, MAX_ATTEMPTS)
        if gave_up:
            logger.error(
                "Outbox job %s (%s %s) failed %s times; giving up",
                job.id, job.kind, job.entity_id, job.attempts, exc_info=exc,
            )  # fmt: skip
        else:
            logger.warning(
                "Outbox job %s (%s %s) failed; retrying in %s",
                job.id, job.kind, job.entity_id, delay, exc_info=exc,
            )  # fmt: skip

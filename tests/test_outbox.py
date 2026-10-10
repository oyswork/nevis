import asyncio
import datetime
import uuid
from collections.abc import AsyncGenerator
from unittest.mock import NonCallableMagicMock

import pytest
from httpx2 import AsyncClient
from meilisearch_python_sdk import AsyncClient as SearchClient

from nevis.config import Settings
from nevis.db import DbPool, create_pool
from nevis.outbox import repository, worker
from nevis.outbox.handlers import create_outbox_worker
from nevis.outbox.repository import OutboxKind
from nevis.outbox.worker import MAX_ATTEMPTS, OutboxWorker, retry_delay
from nevis.search.client import create_search_client
from nevis.search.indexes import CLIENTS_INDEX
from nevis.search.indexing import index_client

CLIENT = {"first_name": "John", "last_name": "Doe", "email": "john.doe@neviswealth.com"}


async def fail(_: uuid.UUID) -> None:
    msg = "search is down"
    raise RuntimeError(msg)


@pytest.mark.unit
def test_retry_delay_doubles_per_attempt_up_to_a_cap() -> None:
    delays = [retry_delay(attempts) for attempts in range(1, 30)]

    assert delays[:3] == [datetime.timedelta(seconds=s) for s in (1, 2, 4)]
    assert delays == sorted(delays)
    assert delays[-1] == worker.RETRY_MAX_DELAY


@pytest.mark.unit
async def test_worker_retries_a_job_of_unknown_kind(fake_pool: NonCallableMagicMock) -> None:
    job = {
        "id": 7,
        "kind": "no_such_kind",
        "entity_id": uuid.uuid4(),
        "attempts": 1,
        "available_at": datetime.datetime.now(datetime.UTC),
        "locked_until": None,
        "last_error": None,
        "failed_at": None,
        "created_at": datetime.datetime.now(datetime.UTC),
    }
    fake_pool.fetch.return_value = [job]
    fake_pool.fetchval.return_value = False

    assert await OutboxWorker(fake_pool, {}).run_once() == 1

    (query, job_id, error, *_), _ = fake_pool.fetchval.call_args
    assert (query, job_id) == (repository.RETRY_JOB, 7)
    assert "no_such_kind" in error


@pytest.mark.unit
async def test_worker_keeps_polling_after_a_failed_iteration(
    fake_pool: NonCallableMagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(worker, "ERROR_PAUSE_SECONDS", 0)
    # The database is down for one claim; the loop must survive it and claim again.
    fake_pool.fetch.side_effect = [ConnectionRefusedError(), asyncio.CancelledError()]

    with pytest.raises(asyncio.CancelledError):
        await OutboxWorker(fake_pool, {}).run_forever()

    assert fake_pool.fetch.call_count == 2


@pytest.mark.unit
async def test_worker_polls_in_the_background_until_stopped(
    fake_pool: NonCallableMagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(worker, "POLL_INTERVAL_SECONDS", 0)
    polled = asyncio.Event()

    async def claim_nothing(*_: object) -> list[object]:
        polled.set()
        return []

    fake_pool.fetch.side_effect = claim_nothing
    outbox_worker = OutboxWorker(fake_pool, {})
    await outbox_worker.stop()  # not started yet: nothing to stop

    outbox_worker.start()
    await asyncio.wait_for(polled.wait(), timeout=5)
    await outbox_worker.stop()

    calls = fake_pool.fetch.call_count
    await asyncio.sleep(0)
    assert fake_pool.fetch.call_count == calls  # stopped: no more polling


@pytest.mark.unit
async def test_app_starts_the_outbox_worker(
    client: AsyncClient, fake_outbox_worker: NonCallableMagicMock
) -> None:
    fake_outbox_worker.start.assert_called_once_with()


@pytest.fixture
async def db_pool(db_settings: Settings) -> AsyncGenerator[DbPool]:
    pool = await create_pool(db_settings)
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture
async def search_client(db_settings: Settings) -> AsyncGenerator[SearchClient]:
    async with create_search_client(db_settings) as client:
        yield client


async def outbox_rows(pool: DbPool) -> list[dict[str, object]]:
    return [dict(row) for row in await pool.fetch("SELECT * FROM outbox ORDER BY id")]


async def enqueue(pool: DbPool, count: int) -> None:
    async with pool.acquire() as connection:
        for _ in range(count):
            await repository.enqueue(connection, OutboxKind.INDEX_CLIENT, uuid.uuid4())


@pytest.mark.integration
async def test_create_client_enqueues_an_index_job(db_client: AsyncClient, db_pool: DbPool) -> None:
    client_id = (await db_client.post("/clients", json=CLIENT)).json()["id"]

    [job] = await outbox_rows(db_pool)

    assert (job["kind"], str(job["entity_id"]), job["attempts"]) == ("index_client", client_id, 0)


@pytest.mark.integration
async def test_rejected_client_enqueues_nothing(db_client: AsyncClient, db_pool: DbPool) -> None:
    await db_client.post("/clients", json=CLIENT)

    assert (await db_client.post("/clients", json=CLIENT)).status_code == 409

    assert len(await outbox_rows(db_pool)) == 1


@pytest.mark.integration
async def test_worker_indexes_the_client_and_deletes_the_job(
    db_client: AsyncClient, db_pool: DbPool, search_client: SearchClient
) -> None:
    client_id = (await db_client.post("/clients", json=CLIENT)).json()["id"]

    processed = await create_outbox_worker(db_pool, search_client).run_once()

    assert processed == 1
    assert await outbox_rows(db_pool) == []
    hits = (await search_client.index(CLIENTS_INDEX).search("neviswealth")).hits
    assert [hit["id"] for hit in hits] == [client_id]


@pytest.mark.integration
async def test_index_client_removes_a_client_that_no_longer_exists(
    db_client: AsyncClient, db_pool: DbPool, search_client: SearchClient
) -> None:
    client_id = (await db_client.post("/clients", json=CLIENT)).json()["id"]
    await create_outbox_worker(db_pool, search_client).run_once()

    await db_pool.execute("DELETE FROM clients WHERE id = $1", uuid.UUID(client_id))
    await index_client(db_pool, search_client, uuid.UUID(client_id))

    assert (await search_client.index(CLIENTS_INDEX).search("neviswealth")).hits == []


@pytest.mark.integration
async def test_failed_job_is_retried_later_and_given_up_after_max_attempts(
    db_client: AsyncClient, db_pool: DbPool
) -> None:
    await enqueue(db_pool, 1)
    failing_worker = OutboxWorker(db_pool, {OutboxKind.INDEX_CLIENT: fail})

    assert await failing_worker.run_once() == 1
    [job] = await outbox_rows(db_pool)
    assert job["attempts"] == 1
    assert job["last_error"] == "RuntimeError: search is down"
    assert job["locked_until"] is None
    assert job["failed_at"] is None
    assert await failing_worker.run_once() == 0  # backing off

    await db_pool.execute("UPDATE outbox SET available_at = now(), attempts = $1", MAX_ATTEMPTS - 1)
    assert await failing_worker.run_once() == 1
    [job] = await outbox_rows(db_pool)
    assert job["failed_at"] is not None
    await db_pool.execute("UPDATE outbox SET available_at = now()")
    assert await failing_worker.run_once() == 0  # given up: never claimed again


@pytest.mark.integration
async def test_claimed_job_is_claimable_again_only_after_its_lease_expires(
    db_client: AsyncClient, db_pool: DbPool
) -> None:
    await enqueue(db_pool, 1)
    lease = datetime.timedelta(minutes=1)

    assert len(await repository.claim_jobs(db_pool, 10, lease)) == 1
    assert await repository.claim_jobs(db_pool, 10, lease) == []

    await db_pool.execute("UPDATE outbox SET locked_until = now() - interval '1 second'")
    [job] = await repository.claim_jobs(db_pool, 10, lease)
    assert job.attempts == 2


@pytest.mark.integration
async def test_concurrent_claims_get_disjoint_jobs(db_client: AsyncClient, db_pool: DbPool) -> None:
    await enqueue(db_pool, 20)
    lease = datetime.timedelta(minutes=1)

    first, second = await asyncio.gather(
        repository.claim_jobs(db_pool, 20, lease), repository.claim_jobs(db_pool, 20, lease)
    )

    first_ids, second_ids = {job.id for job in first}, {job.id for job in second}
    assert first_ids.isdisjoint(second_ids)
    assert len(first_ids | second_ids) == 20

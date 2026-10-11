"""The summary worker: claims unsummarized documents under a lease and writes their summaries."""

import asyncio
import contextlib
import datetime
import logging
import os
import signal
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from unittest.mock import NonCallableMagicMock, create_autospec

import asyncpg
import pytest
from httpx2 import AsyncClient
from pydantic import PostgresDsn, ValidationError

from nevis.config import Settings, WorkerSettings
from nevis.db import DbPool, create_pool
from nevis.db_models import Document
from nevis.documents.schemas import DocumentRead
from nevis.summaries import repository, worker
from nevis.summaries.client import REQUEST_TIMEOUT_SECONDS, Summarizer
from nevis.summaries.worker import SummaryWorker, backoff

CLIENT = {"first_name": "John", "last_name": "Doe", "email": "john.doe@neviswealth.com"}
DOCUMENT = {"title": "Utility bill", "content": "Electricity bill from Vattenfall, CHF 84.20."}
LEASE = datetime.timedelta(minutes=1)
DEADLINE_SECONDS = 10.0
ONE_HOUR = datetime.timedelta(hours=1)


# --- Pure logic and settings (no infrastructure) ---


@pytest.mark.unit
def test_backoff_doubles_per_failed_attempt_up_to_the_cap() -> None:
    delays = [backoff(attempts) for attempts in range(1, 12)]

    assert delays[0] == worker.BACKOFF_BASE
    assert delays[1] == 2 * worker.BACKOFF_BASE
    assert delays == sorted(delays)
    assert delays[-1] == worker.BACKOFF_MAX == ONE_HOUR
    assert backoff(10_000) == worker.BACKOFF_MAX  # no overflow
    assert backoff(0) == worker.BACKOFF_BASE


@pytest.mark.unit
def test_the_model_call_is_bounded_by_a_deadline_shorter_than_the_lease() -> None:
    # The httpx timeout is per phase, so the total deadline is what keeps a call under the lease.
    assert REQUEST_TIMEOUT_SECONDS <= worker.SUMMARY_DEADLINE_SECONDS
    assert datetime.timedelta(seconds=worker.SUMMARY_DEADLINE_SECONDS) < worker.LEASE


@pytest.mark.unit
def test_worker_settings_need_no_embedding_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("NEVIS_SEARCH_RERANK", "NEVIS_MODEL_CACHE_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NEVIS_DATABASE_URL", "postgresql://u@db/nevis")

    settings = WorkerSettings()

    assert str(settings.database_url) == "postgresql://u@db/nevis"
    assert (settings.summary_api_url, settings.summary_model) == (None, None)


@pytest.mark.unit
def test_worker_settings_validate_the_database_scheme_and_the_summary_pair() -> None:
    with pytest.raises(ValidationError, match="plain postgresql://"):
        WorkerSettings(database_url=PostgresDsn("postgresql+asyncpg://u@db/nevis"))
    with pytest.raises(ValidationError, match="NEVIS_SUMMARY_API_URL and NEVIS_SUMMARY_MODEL"):
        WorkerSettings.model_validate(
            {"database_url": "postgresql://u@db/nevis", "summary_model": "llama3.2"}
        )


@pytest.mark.unit
def test_lease_columns_are_not_part_of_the_api_response() -> None:
    assert {"summary_attempts", "summary_locked_until"}.isdisjoint(DocumentRead.model_fields)


# --- Helpers ---


@pytest.fixture
async def client_id(db_client: AsyncClient) -> uuid.UUID:
    return uuid.UUID((await db_client.post("/clients", json=CLIENT)).json()["id"])


async def add_document(
    pool: DbPool, client_id: uuid.UUID, title: str = "Utility bill", *, summary: str | None = None
) -> uuid.UUID:
    """Insert directly: no chunks, no notification (the worker doesn't need either)."""
    document_id: uuid.UUID = await pool.fetchval(
        "INSERT INTO documents (client_id, title, content, summary) VALUES ($1, $2, 'content', $3)"
        " RETURNING id",
        client_id,
        title,
        summary,
    )
    return document_id


async def get_row(pool: DbPool, document_id: uuid.UUID) -> asyncpg.Record:
    row = await pool.fetchrow("SELECT * FROM documents WHERE id = $1", document_id)
    assert row is not None
    return row


async def lock_remaining(pool: DbPool, document_id: uuid.UUID) -> datetime.timedelta | None:
    remaining: datetime.timedelta | None = await pool.fetchval(
        "SELECT summary_locked_until - now() FROM documents WHERE id = $1", document_id
    )
    return remaining


def fake_summarizer(summary: str = "A summary.") -> NonCallableMagicMock:
    summarizer: NonCallableMagicMock = create_autospec(Summarizer, instance=True)
    summarizer.summarize.return_value = summary
    return summarizer


def make_worker(
    db_settings: Settings, pool: DbPool, summarizer: NonCallableMagicMock | None
) -> SummaryWorker:
    return SummaryWorker(pool, summarizer, str(db_settings.database_url))


async def eventually(check: Callable[[], Awaitable[bool]]) -> None:
    """Poll until `check` holds; a deadline, not a fixed sleep."""
    try:
        async with asyncio.timeout(DEADLINE_SECONDS):
            while not await check():
                await asyncio.sleep(0.02)
    except TimeoutError:
        pytest.fail(f"condition not met within {DEADLINE_SECONDS} s")


async def has_summary(pool: DbPool, document_id: uuid.UUID) -> bool:
    return (await get_row(pool, document_id))["summary"] is not None


@contextlib.asynccontextmanager
async def running(summary_worker: SummaryWorker) -> AsyncGenerator[asyncio.Task[None]]:
    task = asyncio.create_task(summary_worker.run_forever())
    try:
        yield task
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def listening(summary_worker: SummaryWorker) -> int:
    """Wait until the worker's LISTEN connection is up; returns its backend pid."""

    async def is_up() -> bool:
        return summary_worker.listener_pid is not None

    await eventually(is_up)
    assert summary_worker.listener_pid is not None
    return summary_worker.listener_pid


@pytest.fixture
def fast_reconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker, "RECONNECT_DELAY_SECONDS", 0.05)
    monkeypatch.setattr(worker, "ERROR_PAUSE_SECONDS", 0.05)


@pytest.fixture
def long_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only notifications can wake the worker within a test."""
    monkeypatch.setattr(worker, "SWEEP_INTERVAL_SECONDS", 3600)


# --- Claim and lease ---


@pytest.mark.integration
async def test_claimed_document_is_claimable_again_only_after_its_lease_expires(
    db_pool: DbPool, client_id: uuid.UUID
) -> None:
    document_id = await add_document(db_pool, client_id)

    claimed = await repository.claim_document(db_pool, LEASE)
    assert claimed is not None
    assert claimed.id == document_id
    assert await repository.claim_document(db_pool, LEASE) is None
    remaining = await lock_remaining(db_pool, document_id)
    assert remaining is not None
    assert datetime.timedelta(seconds=50) < remaining <= LEASE

    await db_pool.execute("UPDATE documents SET summary_locked_until = now() - interval '1 second'")
    reclaimed = await repository.claim_document(db_pool, LEASE)
    assert reclaimed is not None
    assert reclaimed.id == document_id


@pytest.mark.integration
async def test_claim_takes_the_oldest_unsummarized_document_and_skips_summarized_ones(
    db_pool: DbPool, client_id: uuid.UUID
) -> None:
    await add_document(db_pool, client_id, "done", summary="already")
    older = await add_document(db_pool, client_id, "older")
    newer = await add_document(db_pool, client_id, "newer")
    await db_pool.execute(
        "UPDATE documents SET created_at = now() - interval '1 day' WHERE id = $1", older
    )

    first = await repository.claim_document(db_pool, LEASE)
    second = await repository.claim_document(db_pool, LEASE)
    third = await repository.claim_document(db_pool, LEASE)

    assert [first and first.id, second and second.id, third] == [older, newer, None]


@pytest.mark.integration
async def test_concurrent_workers_get_disjoint_documents(
    db_settings: Settings, db_pool: DbPool, client_id: uuid.UUID
) -> None:
    ids = {await add_document(db_pool, client_id, f"doc {n}") for n in range(8)}
    seen: list[list[str]] = [[], []]

    def slow_summarizer(index: int) -> NonCallableMagicMock:
        async def summarize(title: str, _: str) -> str:
            seen[index].append(title)
            await asyncio.sleep(0.05)  # long enough for the other worker to claim meanwhile
            return f"summary of {title}"

        summarizer = fake_summarizer()
        summarizer.summarize.side_effect = summarize
        return summarizer

    counts = await asyncio.gather(
        make_worker(db_settings, db_pool, slow_summarizer(0)).run_once(),
        make_worker(db_settings, db_pool, slow_summarizer(1)).run_once(),
    )

    assert sum(counts) == len(ids)
    assert seen[0]
    assert seen[1]  # both did some of the work
    assert set(seen[0]).isdisjoint(seen[1])
    assert len(seen[0]) + len(seen[1]) == len(ids)
    assert await db_pool.fetchval("SELECT count(*) FROM documents WHERE summary IS NULL") == 0


# --- Summarizing ---


@pytest.mark.integration
async def test_run_once_summarizes_every_claimable_document_and_releases_the_lease(
    db_settings: Settings, db_pool: DbPool, client_id: uuid.UUID
) -> None:
    first = await add_document(db_pool, client_id, "first")
    second = await add_document(db_pool, client_id, "second")
    summarizer = fake_summarizer("An electricity bill.")

    count = await make_worker(db_settings, db_pool, summarizer).run_once()

    assert count == 2
    assert {call.args[0] for call in summarizer.summarize.await_args_list} == {"first", "second"}
    for document_id in (first, second):
        row = await get_row(db_pool, document_id)
        assert row["summary"] == "An electricity bill."
        assert row["summary_locked_until"] is None
        assert row["summary_attempts"] == 0
    assert await make_worker(db_settings, db_pool, summarizer).run_once() == 0


@pytest.mark.integration
async def test_summary_is_written_once(
    db_settings: Settings, db_pool: DbPool, client_id: uuid.UUID
) -> None:
    document_id = await add_document(db_pool, client_id)
    summarizer = fake_summarizer("the slow summary")

    async def somebody_else_wins(*_: str) -> str:
        await db_pool.execute("UPDATE documents SET summary = 'first!' WHERE id = $1", document_id)
        return "the slow summary"

    summarizer.summarize.side_effect = somebody_else_wins

    assert await make_worker(db_settings, db_pool, summarizer).run_once() == 1

    assert (await get_row(db_pool, document_id))["summary"] == "first!"
    assert await repository.set_summary(db_pool, document_id, "another") is False
    assert (await get_row(db_pool, document_id))["summary"] == "first!"


@pytest.mark.integration
async def test_document_deleted_meanwhile_is_not_an_error(
    db_settings: Settings,
    db_pool: DbPool,
    client_id: uuid.UUID,
    caplog: pytest.LogCaptureFixture,
) -> None:
    document_id = await add_document(db_pool, client_id)
    summarizer = fake_summarizer()

    async def delete_then_answer(*_: str) -> str:
        await db_pool.execute("DELETE FROM documents WHERE id = $1", document_id)
        return "too late"

    summarizer.summarize.side_effect = delete_then_answer

    with caplog.at_level(logging.DEBUG, logger=worker.logger.name):
        assert await make_worker(db_settings, db_pool, summarizer).run_once() == 1

    assert await db_pool.fetchval("SELECT count(*) FROM documents") == 0
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]
    assert any(str(document_id) in record.getMessage() for record in caplog.records)


# --- Failure and backoff ---


@pytest.mark.integration
async def test_failed_summary_counts_the_attempt_and_backs_off(
    db_settings: Settings,
    db_pool: DbPool,
    client_id: uuid.UUID,
    caplog: pytest.LogCaptureFixture,
) -> None:
    document_id = await add_document(db_pool, client_id)
    summarizer = fake_summarizer()
    summarizer.summarize.side_effect = RuntimeError("model is down")
    summary_worker = make_worker(db_settings, db_pool, summarizer)

    with caplog.at_level(logging.WARNING, logger=worker.logger.name):
        assert await summary_worker.run_once() == 1

    row = await get_row(db_pool, document_id)
    assert (row["summary"], row["summary_attempts"]) == (None, 1)
    remaining = await lock_remaining(db_pool, document_id)
    assert remaining is not None
    assert remaining <= worker.BACKOFF_BASE
    assert remaining > worker.BACKOFF_BASE - datetime.timedelta(seconds=5)
    [warning] = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert str(document_id) in warning.getMessage()
    assert "RuntimeError" in warning.getMessage()
    assert "model is down" not in warning.getMessage()  # may be document-derived text
    assert warning.exc_info is not None  # the traceback is kept
    assert await summary_worker.run_once() == 0  # backing off: not claimed again

    await db_pool.execute("UPDATE documents SET summary_locked_until = now()")
    assert await summary_worker.run_once() == 1
    assert (await get_row(db_pool, document_id))["summary_attempts"] == 2
    second_delay = await lock_remaining(db_pool, document_id)
    assert second_delay is not None
    assert second_delay > worker.BACKOFF_BASE  # doubled


@pytest.mark.integration
async def test_backoff_is_capped_at_one_hour(
    db_settings: Settings, db_pool: DbPool, client_id: uuid.UUID
) -> None:
    document_id = await add_document(db_pool, client_id)
    await db_pool.execute("UPDATE documents SET summary_attempts = 500")
    summarizer = fake_summarizer()
    summarizer.summarize.side_effect = RuntimeError("still down")

    await make_worker(db_settings, db_pool, summarizer).run_once()

    assert (await get_row(db_pool, document_id))["summary_attempts"] == 501
    remaining = await lock_remaining(db_pool, document_id)
    assert remaining is not None
    assert ONE_HOUR - datetime.timedelta(seconds=30) < remaining <= ONE_HOUR


@pytest.mark.integration
async def test_failure_of_one_document_does_not_stop_the_others(
    db_settings: Settings, db_pool: DbPool, client_id: uuid.UUID
) -> None:
    bad = await add_document(db_pool, client_id, "bad")
    good = await add_document(db_pool, client_id, "good")
    summarizer = fake_summarizer()

    async def summarize(title: str, _: str) -> str:
        if title == "bad":
            msg = "cannot summarize"
            raise ValueError(msg)
        return "fine"

    summarizer.summarize.side_effect = summarize

    assert await make_worker(db_settings, db_pool, summarizer).run_once() == 2

    assert (await get_row(db_pool, bad))["summary"] is None
    assert (await get_row(db_pool, good))["summary"] == "fine"


# --- Unconfigured ---


@pytest.mark.integration
async def test_without_a_model_the_worker_claims_nothing_and_idles(
    db_settings: Settings,
    db_pool: DbPool,
    client_id: uuid.UUID,
    caplog: pytest.LogCaptureFixture,
) -> None:
    document_id = await add_document(db_pool, client_id)
    unconfigured = make_worker(db_settings, db_pool, None)

    assert await unconfigured.run_once() == 0

    with caplog.at_level(logging.INFO, logger=worker.logger.name):
        async with running(unconfigured) as task:

            async def announced() -> bool:
                return any("summaries disabled" in r.getMessage() for r in caplog.records)

            await eventually(announced)
            await asyncio.sleep(0)  # let the task run on, if it were going to
            assert not task.done()
            assert unconfigured.listener_pid is None  # no LISTEN needed either
    messages = [r.getMessage() for r in caplog.records if r.name == worker.logger.name]
    assert messages == ["summaries disabled: NEVIS_SUMMARY_* not set"]
    row = await get_row(db_pool, document_id)
    assert (row["summary_locked_until"], row["summary_attempts"]) == (None, 0)


# --- Wake-up: notifications, sweep, reconnect ---


@pytest.mark.integration
@pytest.mark.usefixtures("long_sweep")
async def test_a_new_document_wakes_the_worker_through_a_notification(
    db_settings: Settings, db_pool: DbPool, db_client: AsyncClient, client_id: uuid.UUID
) -> None:
    summary_worker = make_worker(db_settings, db_pool, fake_summarizer("An electricity bill."))

    async with running(summary_worker):
        await listening(summary_worker)
        response = await db_client.post(f"/clients/{client_id}/documents", json=DOCUMENT)
        document_id = uuid.UUID(response.json()["id"])

        async def summarized() -> bool:
            return await has_summary(db_pool, document_id)

        await eventually(summarized)

    assert (await get_row(db_pool, document_id))["summary"] == "An electricity bill."
    assert summary_worker.listener_pid is None  # cancelling closed the listener connection


@pytest.mark.integration
async def test_the_sweep_catches_documents_that_were_never_announced(
    db_settings: Settings,
    db_pool: DbPool,
    client_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker, "SWEEP_INTERVAL_SECONDS", 0.05)
    summary_worker = make_worker(db_settings, db_pool, fake_summarizer())
    unannounced = await add_document(db_pool, client_id, "unannounced")
    # A crashed worker's document: its lease expires shortly, after the worker has started.
    expiring = await add_document(db_pool, client_id, "expiring")
    await db_pool.execute(
        "UPDATE documents SET summary_locked_until = now() + interval '0.3 second' WHERE id = $1",
        expiring,
    )

    async with running(summary_worker):

        async def both_summarized() -> bool:
            return await has_summary(db_pool, unannounced) and await has_summary(db_pool, expiring)

        await eventually(both_summarized)


@pytest.mark.integration
@pytest.mark.usefixtures("long_sweep", "fast_reconnect")
async def test_listener_reconnects_after_its_connection_is_terminated_and_still_wakes(
    db_settings: Settings,
    db_pool: DbPool,
    db_client: AsyncClient,
    client_id: uuid.UUID,
    caplog: pytest.LogCaptureFixture,
) -> None:
    summary_worker = make_worker(db_settings, db_pool, fake_summarizer())

    with caplog.at_level(logging.WARNING, logger=worker.logger.name):
        async with running(summary_worker):
            first_pid = await listening(summary_worker)
            await db_pool.execute("SELECT pg_terminate_backend($1)", first_pid)

            async def reconnected() -> bool:
                return summary_worker.listener_pid not in (None, first_pid)

            await eventually(reconnected)

            response = await db_client.post(f"/clients/{client_id}/documents", json=DOCUMENT)
            document_id = uuid.UUID(response.json()["id"])

            async def summarized() -> bool:
                return await has_summary(db_pool, document_id)

            await eventually(summarized)

    assert any("listener" in record.getMessage().lower() for record in caplog.records)


@pytest.mark.integration
@pytest.mark.usefixtures("long_sweep", "fast_reconnect")
async def test_listener_keeps_retrying_while_the_database_is_unreachable(
    db_settings: Settings, db_pool: DbPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_connect = asyncpg.connect
    attempts = 0

    async def flaky_connect(dsn: str, *, timeout: float) -> asyncpg.Connection[asyncpg.Record]:
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise ConnectionRefusedError
        return await real_connect(dsn, timeout=timeout)

    monkeypatch.setattr(asyncpg, "connect", flaky_connect)
    summary_worker = make_worker(db_settings, db_pool, fake_summarizer())

    async with running(summary_worker):
        await listening(summary_worker)

    assert attempts == 3


@pytest.mark.integration
@pytest.mark.usefixtures("fast_reconnect")
async def test_loop_survives_a_failing_iteration(
    db_settings: Settings,
    db_pool: DbPool,
    client_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(worker, "SWEEP_INTERVAL_SECONDS", 0.05)
    real_claim = repository.claim_document
    failures = 0

    async def claim_failing_once(pool: DbPool, lease: datetime.timedelta) -> object:
        nonlocal failures
        failures += 1
        if failures == 1:
            msg = "database went away"
            raise OSError(msg)
        return await real_claim(pool, lease)

    monkeypatch.setattr(repository, "claim_document", claim_failing_once)
    document_id = await add_document(db_pool, client_id)

    with caplog.at_level(logging.ERROR, logger=worker.logger.name):
        async with running(make_worker(db_settings, db_pool, fake_summarizer())):

            async def summarized() -> bool:
                return await has_summary(db_pool, document_id)

            await eventually(summarized)

    assert any(record.exc_info for record in caplog.records)


# --- Entrypoint ---


@pytest.mark.integration
async def test_run_closes_everything_on_shutdown(
    db_settings: Settings, db_pool: DbPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    summarizer = fake_summarizer()
    created_pools: list[DbPool] = []
    pool_created = asyncio.Event()
    real_create_pool = create_pool

    async def tracked_create_pool(settings: WorkerSettings) -> DbPool:
        pool = await real_create_pool(settings)
        created_pools.append(pool)
        pool_created.set()
        return pool

    monkeypatch.setattr(worker, "create_pool", tracked_create_pool)
    monkeypatch.setattr(worker, "create_summarizer", lambda _: summarizer)
    stop = asyncio.Event()
    run = asyncio.create_task(
        worker.run(WorkerSettings.model_validate(db_settings.model_dump()), stop)
    )

    try:
        async with asyncio.timeout(DEADLINE_SECONDS):
            await pool_created.wait()
        assert not run.done()  # runs until told to stop
        stop.set()
        async with asyncio.timeout(DEADLINE_SECONDS):
            await run
    finally:
        run.cancel()
        await asyncio.gather(run, return_exceptions=True)

    summarizer.aclose.assert_awaited_once()
    [pool] = created_pools
    assert pool.is_closing()


@pytest.mark.unit
async def test_sigterm_and_sigint_set_the_stop_event() -> None:
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        stop = asyncio.Event()
        worker.stop_on_signals(loop, stop)
        try:
            os.kill(os.getpid(), signum)
            async with asyncio.timeout(DEADLINE_SECONDS):
                await stop.wait()
        finally:
            loop.remove_signal_handler(signal.SIGTERM)
            loop.remove_signal_handler(signal.SIGINT)


# --- The api's side ---


@pytest.mark.integration
async def test_create_document_notifies_the_worker_and_enqueues_no_summary_job(
    db_settings: Settings, db_pool: DbPool, db_client: AsyncClient, client_id: uuid.UUID
) -> None:
    received: asyncio.Queue[str] = asyncio.Queue()
    listener = await asyncpg.connect(str(db_settings.database_url))
    try:
        await listener.add_listener(
            repository.SUMMARY_CHANNEL,
            lambda _c, _pid, _channel, payload: received.put_nowait(payload),
        )

        response = await db_client.post(f"/clients/{client_id}/documents", json=DOCUMENT)

        async with asyncio.timeout(DEADLINE_SECONDS):
            assert await received.get() == response.json()["id"]
    finally:
        await listener.close()


@pytest.mark.integration
async def test_rejected_document_sends_no_notification(
    db_settings: Settings, db_client: AsyncClient
) -> None:
    received: asyncio.Queue[str] = asyncio.Queue()
    listener = await asyncpg.connect(str(db_settings.database_url))
    try:
        await listener.add_listener(
            repository.SUMMARY_CHANNEL,
            lambda _c, _pid, _channel, payload: received.put_nowait(payload),
        )

        response = await db_client.post(f"/clients/{uuid.uuid4()}/documents", json=DOCUMENT)
        await listener.execute("SELECT 1")  # a round trip: pending notifications have arrived

        assert response.status_code == 404
        assert received.empty()
    finally:
        await listener.close()


@pytest.mark.unit
async def test_run_closes_the_pool_when_startup_fails(
    fake_pool: NonCallableMagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def create_fake_pool(_: WorkerSettings) -> NonCallableMagicMock:
        return fake_pool

    def broken_summarizer(_: object) -> None:
        msg = "cannot build the summarizer"
        raise RuntimeError(msg)

    monkeypatch.setattr(worker, "create_pool", create_fake_pool)
    monkeypatch.setattr(worker, "create_summarizer", broken_summarizer)
    settings = WorkerSettings(database_url=PostgresDsn("postgresql://unused@unused.invalid/x"))

    with pytest.raises(RuntimeError, match="cannot build the summarizer"):
        await worker.run(settings, asyncio.Event())

    fake_pool.close.assert_awaited_once()


# --- Model deadline and stale failure writes ---


def claimed_document(title: str) -> Document:
    return Document(
        id=uuid.uuid4(),
        client_id=uuid.uuid4(),
        title=title,
        content="content",
        summary=None,
        summary_attempts=0,
        summary_locked_until=datetime.datetime.now(datetime.UTC),
        created_at=datetime.datetime.now(datetime.UTC),
    )


@pytest.mark.unit
async def test_a_model_call_that_outlives_the_deadline_is_a_recorded_failure(
    fake_pool: NonCallableMagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(worker, "SUMMARY_DEADLINE_SECONDS", 0.05)
    stuck, fine = claimed_document("stuck"), claimed_document("fine")
    queue: list[Document | None] = [stuck, fine, None]

    async def claim(*_: object) -> Document | None:
        return queue.pop(0)

    monkeypatch.setattr(repository, "claim_document", claim)
    fake_pool.execute.return_value = "UPDATE 1"
    summarizer = fake_summarizer()

    async def summarize(title: str, _: str) -> str:
        if title == "stuck":
            await asyncio.sleep(3600)  # a model trickling bytes: no httpx phase ever times out
        return "fine summary"

    summarizer.summarize.side_effect = summarize

    count = await SummaryWorker(fake_pool, summarizer, "unused").run_once()

    assert count == 2  # the worker went on to the next document
    statements = [(call.args[0], call.args[1]) for call in fake_pool.execute.await_args_list]
    assert statements == [
        (repository.RECORD_FAILURE, stuck.id),
        (repository.SET_SUMMARY, fine.id),
    ]
    assert fake_pool.execute.await_args_list[0].args[3] == stuck.summary_locked_until


@pytest.mark.integration
async def test_a_stale_failure_does_not_overwrite_a_newer_lease(
    db_pool: DbPool, client_id: uuid.UUID
) -> None:
    document_id = await add_document(db_pool, client_id)
    first = await repository.claim_document(db_pool, LEASE)
    assert first is not None
    await db_pool.execute("UPDATE documents SET summary_locked_until = now() - interval '1 second'")
    second = await repository.claim_document(db_pool, LEASE)  # another worker, after expiry
    assert second is not None
    assert second.summary_locked_until != first.summary_locked_until
    before = await get_row(db_pool, document_id)

    stale = await repository.record_failure(
        db_pool, document_id, worker.BACKOFF_BASE, first.summary_locked_until
    )

    after = await get_row(db_pool, document_id)
    assert stale is False
    assert (after["summary_locked_until"], after["summary_attempts"]) == (
        before["summary_locked_until"],
        before["summary_attempts"],
    )
    current = await repository.record_failure(
        db_pool, document_id, worker.BACKOFF_BASE, second.summary_locked_until
    )
    assert current is True
    assert (await get_row(db_pool, document_id))["summary_attempts"] == 1

"""The summary worker: ``python -m nevis.summaries.worker``.

A separate process from the api. It claims documents without a summary one at a time under a
lease, summarizes them with the model and writes the summary once. It is woken by
LISTEN/NOTIFY when the api stores a document, and sweeps periodically in any case, so a missed
notification, a dropped connection or an expired lease only delays a summary.

A document that keeps failing is retried forever, at most once per BACKOFF_MAX; its
documents.summary_attempts shows how often it failed. There is no give-up state: a document
that can't be summarized costs one model call an hour, and fixing the cause (model, network)
needs no manual requeue.
"""

import asyncio
import contextlib
import datetime
import logging
import signal

import asyncpg

from nevis.config import WorkerSettings
from nevis.db import DbPool, create_pool
from nevis.db_models import Document
from nevis.summaries import repository
from nevis.summaries.client import Summarizer, create_summarizer

logger = logging.getLogger(__name__)

# Longer than the model request timeout (summaries.client.REQUEST_TIMEOUT_SECONDS, 90 s), so a
# document is never summarized twice at once; one that outlives it is still written once.
LEASE = datetime.timedelta(minutes=2)
# Total time for one model call. The client's httpx timeout is per phase (connect, each read), so
# a model trickling bytes could outlive it; this deadline is what keeps a call under the lease.
SUMMARY_DEADLINE_SECONDS = 100
BACKOFF_BASE = datetime.timedelta(seconds=30)
BACKOFF_MAX = datetime.timedelta(hours=1)
# Doubling stops here, long after BACKOFF_MAX is reached, so the delay can't overflow.
BACKOFF_MAX_DOUBLINGS = 20
# How long to wait for a notification before looking for work anyway: this bounds the delay of a
# missed notification, an expired lease and a backed-off document that has become due.
SWEEP_INTERVAL_SECONDS = 60
# After an iteration of the worker itself fails (e.g. the database is down), not a document.
ERROR_PAUSE_SECONDS = 5
# Before reconnecting the LISTEN connection.
RECONNECT_DELAY_SECONDS = 5
CONNECT_TIMEOUT_SECONDS = 5
# Bounds closing the LISTEN connection, so a dead peer can't hang shutdown or a reconnect.
LISTENER_CLOSE_TIMEOUT_SECONDS = 5


def backoff(attempts: int) -> datetime.timedelta:
    """Exponential delay after the given number of failed attempts, capped at BACKOFF_MAX."""
    doublings = min(max(attempts - 1, 0), BACKOFF_MAX_DOUBLINGS)
    return min(BACKOFF_BASE * (1 << doublings), BACKOFF_MAX)


class SummaryWorker:
    """Without a summarizer (no model configured) it claims nothing and only idles."""

    def __init__(self, pool: DbPool, summarizer: Summarizer | None, database_url: str) -> None:
        self._pool = pool
        self._summarizer = summarizer
        self._database_url = database_url
        self._wakeup = asyncio.Event()
        self._listener: asyncpg.Connection[asyncpg.Record] | None = None

    @property
    def listener_pid(self) -> int | None:
        """Backend pid of the LISTEN connection while it is up, else None."""
        return None if self._listener is None else self._listener.get_server_pid()

    async def run_forever(self) -> None:
        """Summarize until cancelled; survives any failure of an iteration."""
        if self._summarizer is None:
            logger.info("summaries disabled: NEVIS_SUMMARY_* not set")
            await asyncio.Event().wait()  # idle until cancelled: nothing to poll or listen for
        listener = asyncio.create_task(self._listen_forever(), name="summary-listener")
        try:
            while True:
                # Clear before draining: a notification during the drain makes the next wait
                # return at once, instead of being lost.
                self._wakeup.clear()
                try:
                    await self.run_once()
                except Exception:
                    logger.exception("Summary worker iteration failed; retrying shortly")
                    await asyncio.sleep(ERROR_PAUSE_SECONDS)
                    continue
                with contextlib.suppress(TimeoutError):
                    async with asyncio.timeout(SWEEP_INTERVAL_SECONDS):
                        await self._wakeup.wait()
        finally:
            listener.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await listener

    async def run_once(self) -> int:
        """Summarize every document that is claimable now; returns how many were claimed."""
        if self._summarizer is None:
            return 0
        count = 0
        while (document := await repository.claim_document(self._pool, LEASE)) is not None:
            await self._summarize(self._summarizer, document)
            count += 1
        return count

    async def _summarize(self, summarizer: Summarizer, document: Document) -> None:
        try:
            async with asyncio.timeout(SUMMARY_DEADLINE_SECONDS):
                summary = await summarizer.summarize(document.title, document.content)
        except Exception as exc:
            delay = backoff(document.summary_attempts + 1)
            recorded = await repository.record_failure(
                self._pool, document.id, delay, document.summary_locked_until
            )
            # Only the type in the message: the error text can carry document-derived content
            # (a validation error embeds the model's reply). The traceback goes with exc_info.
            logger.warning(
                "Summarizing document %s failed (attempt %s, %s); retrying in %s",
                document.id, document.summary_attempts + 1, type(exc).__name__, delay,
                exc_info=exc,
            )  # fmt: skip
            if not recorded:
                logger.debug(
                    "Document %s was re-claimed or gone; failure not recorded", document.id
                )
            return
        if await repository.set_summary(self._pool, document.id, summary):
            logger.info("Summarized document %s", document.id)
        else:
            logger.debug("Document %s was deleted or summarized meanwhile", document.id)

    async def _listen_forever(self) -> None:
        """Keep a dedicated connection on LISTEN; reconnect after a delay when it drops."""
        while True:
            try:
                await self._listen_until_closed()
            except Exception as exc:
                logger.warning(
                    "Summary listener failed (%s: %s); reconnecting in %s s",
                    type(exc).__name__, exc, RECONNECT_DELAY_SECONDS,
                )  # fmt: skip
            else:
                logger.warning(
                    "Summary listener connection closed; reconnecting in %s s",
                    RECONNECT_DELAY_SECONDS,
                )
            await asyncio.sleep(RECONNECT_DELAY_SECONDS)

    async def _listen_until_closed(self) -> None:
        dropped = asyncio.Event()
        connection = await asyncpg.connect(self._database_url, timeout=CONNECT_TIMEOUT_SECONDS)
        try:
            connection.add_termination_listener(lambda _: dropped.set())
            await connection.add_listener(repository.SUMMARY_CHANNEL, self._on_notification)
            self._listener = connection
            # Documents stored while there was no connection sent their notification nowhere.
            self._wakeup.set()
            logger.info("Listening on %s", repository.SUMMARY_CHANNEL)
            # A silent half-open drop (no FIN or RST) isn't detected here; the sweep bounds the
            # delay it causes, as it does for any missed notification.
            await dropped.wait()
        finally:
            self._listener = None
            await connection.close(timeout=LISTENER_CLOSE_TIMEOUT_SECONDS)

    def _on_notification(
        self, _connection: object, _pid: int, _channel: str, _payload: object
    ) -> None:
        self._wakeup.set()


def stop_on_signals(loop: asyncio.AbstractEventLoop, stop: asyncio.Event) -> None:
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)


async def _cancel(*tasks: asyncio.Task[object]) -> None:
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)  # a crash was already reported


async def run(settings: WorkerSettings, stop: asyncio.Event) -> None:
    """Run until `stop` is set, then close the listener, the pool and the summarizer.

    Whatever was opened is closed, in reverse order, also if a later step of the startup fails.
    """
    async with contextlib.AsyncExitStack() as resources:
        pool = await create_pool(settings)
        resources.push_async_callback(pool.close)
        summarizer = create_summarizer(settings)
        if summarizer is not None:
            resources.push_async_callback(summarizer.aclose)
        worker = SummaryWorker(pool, summarizer, str(settings.database_url))
        task = asyncio.create_task(worker.run_forever(), name="summary-worker")
        stopped = asyncio.create_task(stop.wait())
        # Runs first (last in): an in-flight model call is abandoned and its lease expires, so
        # the document is retried later.
        resources.push_async_callback(_cancel, task, stopped)
        # A crash of the loop itself (it handles per-iteration failures) must end the process
        # too, so the restart policy of compose takes over.
        await asyncio.wait({task, stopped}, return_when=asyncio.FIRST_COMPLETED)
        if task.done():
            task.result()


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    stop = asyncio.Event()
    stop_on_signals(asyncio.get_running_loop(), stop)
    await run(WorkerSettings(), stop)


if __name__ == "__main__":
    asyncio.run(main())

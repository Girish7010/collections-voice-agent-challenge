"""Processes one call event end to end, and replays dead letters.

    claim event_id in the ledger          (duplicate? return the stored result)
      -> resilient pipeline call          (retries, timeouts, circuit breaker)
      -> success: mark ledger succeeded, resolve any DLQ row
      -> failure: write to the DLQ, then release the claim

If writing to the DLQ itself fails, the error is raised (the API returns 500)
instead of being swallowed, so the caller knows the event was NOT stored.

All SQLite work runs on ONE dedicated thread (`EventProcessor.db`). SQLite allows
a single writer anyway; spreading writes over the default thread pool caused
write-lock contention and GIL competition that starved the event loop under
load (measured: 0.3 s pipeline calls observed as 2 s+ timeouts at ~65 calls/s).
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import Enum

from app.dependency_adapter import PipelineRequest
from app.dlq import DeadLetterQueue
from app.idempotency import ClaimResult, EventLedger
from app.metrics import Metrics
from app.models import CallEvent
from app.resilience import ResilientPipelineClient

logger = logging.getLogger(__name__)


class ProcessStatus(str, Enum):
    PROCESSED = "processed"
    DUPLICATE = "duplicate"
    IN_PROGRESS = "in_progress"
    DEAD_LETTERED = "dead_lettered"


@dataclass(frozen=True)
class ProcessOutcome:
    status: ProcessStatus
    event_id: str
    attempts: int = 0
    failure_reason: str | None = None
    result: dict | None = None


class EventProcessor:
    def __init__(
        self,
        client: ResilientPipelineClient,
        ledger: EventLedger,
        dlq: DeadLetterQueue,
        metrics: Metrics,
        *,
        claim_lease_s: float = 30.0,
        max_concurrency: int = 200,
        db_threads: int = 1,
    ):
        self.client = client
        self.ledger = ledger
        self.dlq = dlq
        self.metrics = metrics
        self.claim_lease_s = claim_lease_s
        self._bulkhead = asyncio.Semaphore(max_concurrency)
        # One thread is mandatory for SQLite (concurrent schema init and the WAL
        # pragma deadlock). DynamoDB calls are network round-trips, so a single
        # thread would serialize them; Settings.db_threads widens the pool there.
        self._db_thread = ThreadPoolExecutor(max_workers=db_threads, thread_name_prefix="store")

    async def db(self, fn, *args, **kwargs):
        """Run a blocking store call on the store thread pool."""
        return await asyncio.get_running_loop().run_in_executor(self._db_thread, functools.partial(fn, *args, **kwargs))

    def close(self) -> None:
        self._db_thread.shutdown(wait=True)

    async def process(self, event: CallEvent, *, source: str = "live") -> ProcessOutcome:
        event_id = event.event_id
        claim = await self.db(self.ledger.try_claim, event_id, self.claim_lease_s)
        if claim.result is ClaimResult.DUPLICATE:
            self.metrics.record_call("duplicate", source)
            return ProcessOutcome(ProcessStatus.DUPLICATE, event_id, result=claim.stored_result)
        if claim.result is ClaimResult.IN_PROGRESS:
            self.metrics.record_call("in_progress", source)
            return ProcessOutcome(ProcessStatus.IN_PROGRESS, event_id)

        self.metrics.call_started()
        started = time.perf_counter()
        try:
            async with self._bulkhead:
                outcome = await self.client.process(PipelineRequest(event_id, event.audio_ref))
            elapsed_ms = (time.perf_counter() - started) * 1000

            if outcome.ok:
                result = {
                    "transcript": outcome.result.transcript,
                    "response_audio_ref": outcome.result.response_audio_ref,
                    "dependency_latency_ms": outcome.result.latency_ms,
                    "attempts": outcome.attempts,
                }
                await self.db(self.ledger.complete, event_id, result)
                resolved_by = "replay" if source == "replay" else "resubmission"
                await self.db(self.dlq.resolve, event_id, resolved_by=resolved_by)
                self.metrics.record_call("processed", source, elapsed_ms)
                logger.info("call processed", extra={"event_id": event_id, "source": source,
                                                     "attempts": outcome.attempts, "latency_ms": round(elapsed_ms, 1)})
                return ProcessOutcome(ProcessStatus.PROCESSED, event_id, outcome.attempts, result=result)

            await self.db(self.dlq.add, event.model_dump(mode="json"),
                                    reason=outcome.failure_reason, attempts=outcome.attempts)
            await self.db(self.ledger.release, event_id)
            self.metrics.record_call("dead_lettered", source)
            logger.warning("call dead-lettered", extra={"event_id": event_id, "source": source,
                                                        "attempts": outcome.attempts, "reason": outcome.failure_reason})
            return ProcessOutcome(ProcessStatus.DEAD_LETTERED, event_id, outcome.attempts, outcome.failure_reason)
        except BaseException:
            logger.critical("event not persisted; releasing claim", extra={"event_id": event_id}, exc_info=True)
            await asyncio.shield(self.db(self.ledger.release, event_id))
            raise
        finally:
            self.metrics.call_finished()


async def replay_dead_letters(
    processor: EventProcessor,
    dlq: DeadLetterQueue,
    *,
    event_ids: list[str] | None = None,
    limit: int = 100,
    concurrency: int = 1,
    lease_s: float = 60.0,
) -> dict:
    """Re-run pending dead letters through the normal processing path.

    Safe to run repeatedly or in parallel: each row must be claimed first, and
    the ledger stops an event that already succeeded from being processed again.
    Stops early if the circuit breaker is open, instead of churning the queue.
    """
    summary = {"attempted": 0, "resolved": 0, "already_processed": 0, "failed_again": 0,
               "skipped_claimed": 0, "not_found": [], "stopped_reason": None}

    if event_ids is not None:
        candidates = []
        for eid in event_ids:
            row = await processor.db(dlq.get, eid)
            if row is None:
                summary["not_found"].append(eid)
            else:
                candidates.append(row)
    else:
        candidates = await processor.db(dlq.list, "pending", limit)

    queue: asyncio.Queue = asyncio.Queue()
    for row in candidates:
        queue.put_nowait(row)
    stop = asyncio.Event()

    async def worker() -> None:
        while not stop.is_set():
            try:
                row = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            if not await processor.db(dlq.claim, row.event_id, lease_s):
                summary["skipped_claimed"] += 1
                continue
            summary["attempted"] += 1
            try:
                outcome = await processor.process(CallEvent.model_validate(row.payload), source="replay")
            except BaseException:
                await asyncio.shield(processor.db(dlq.unclaim, row.event_id))
                raise
            if outcome.status is ProcessStatus.PROCESSED:
                summary["resolved"] += 1
            elif outcome.status is ProcessStatus.DUPLICATE:
                await processor.db(dlq.resolve, row.event_id, resolved_by="already_processed")
                summary["already_processed"] += 1
            elif outcome.status is ProcessStatus.IN_PROGRESS:
                await processor.db(dlq.unclaim, row.event_id)
                summary["skipped_claimed"] += 1
            else:
                summary["failed_again"] += 1
                if outcome.failure_reason == "circuit_open":
                    summary["stopped_reason"] = "circuit_open"
                    stop.set()

    await asyncio.gather(*(worker() for _ in range(max(1, concurrency))))
    logger.info("dlq replay finished", extra={"summary": summary})
    return summary

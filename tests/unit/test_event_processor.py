"""EventProcessor ties ledger + resilient client + DLQ together; replay goes through it."""

import asyncio
import random

import pytest

from app.circuit_breaker import CircuitBreaker
from app.dependency_adapter import DependencyError, PipelineResult
from app.dlq import DeadLetterQueue
from app.event_processor import EventProcessor, ProcessStatus, replay_dead_letters
from app.idempotency import EventLedger
from app.metrics import Metrics
from app.models import CallEvent
from app.resilience import ResilientPipelineClient, RetryPolicy

OK = PipelineResult(transcript="synthetic transcript", response_audio_ref="synthetic", latency_ms=50)
FAIL = DependencyError("downstream_timeout", retryable=True, status_code=503)


def event(i=1, **kw) -> CallEvent:
    return CallEvent.model_validate({
        "event_id": f"evt-{i}", "campaign_id": "CAMP-2026-09-A", "borrower_bucket": "0-30 DPD",
        "product_type": "bnpl", "timestamp": "2026-09-01T09:03:12Z", "call_duration_s": 47,
        "outcome": "connected", "retry_count": 0, **kw,
    })


class SwitchableAdapter:
    """Fails while `.failing` is True; counts calls per event_id."""

    def __init__(self, failing=False, delay=0.0):
        self.failing = failing
        self.delay = delay
        self.calls: dict[str, int] = {}

    async def process(self, request):
        self.calls[request.event_id] = self.calls.get(request.event_id, 0) + 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.failing:
            raise FAIL
        return OK


async def no_sleep(_):
    return None


@pytest.fixture
def parts(tmp_path):
    db = str(tmp_path / "svc.db")
    adapter = SwitchableAdapter()
    metrics = Metrics()
    breaker = CircuitBreaker(window_size=20, min_calls=10, failure_rate_threshold=0.5, open_duration_s=0)
    client = ResilientPipelineClient(adapter, breaker, RetryPolicy(max_attempts=3, base_delay_s=0, max_delay_s=0, attempt_timeout_s=1),
                                     metrics=metrics, sleep=no_sleep, rng=random.Random(1))
    dlq = DeadLetterQueue(db)
    processor = EventProcessor(client, EventLedger(db), dlq, metrics)
    return processor, adapter, dlq, metrics


async def test_successful_call_is_processed(parts):
    processor, adapter, dlq, metrics = parts

    outcome = await processor.process(event())

    assert outcome.status is ProcessStatus.PROCESSED
    assert outcome.result["transcript"] == "synthetic transcript"
    assert dlq.depth() == 0
    assert metrics.calls["live:processed"] == 1


async def test_exhausted_call_is_dead_lettered_with_details(parts):
    processor, adapter, dlq, metrics = parts
    adapter.failing = True

    outcome = await processor.process(event())

    assert outcome.status is ProcessStatus.DEAD_LETTERED
    assert outcome.failure_reason == "downstream_timeout"
    row = dlq.get("evt-1")
    assert row.status == "pending"
    assert row.attempts == 3 and row.retry_count == 2
    assert row.campaign_id == "CAMP-2026-09-A"
    assert metrics.calls["live:dead_lettered"] == 1


async def test_resubmitting_a_processed_event_does_not_call_dependency_again(parts):
    processor, adapter, dlq, metrics = parts
    await processor.process(event())

    outcome = await processor.process(event())

    assert outcome.status is ProcessStatus.DUPLICATE
    assert outcome.result["transcript"] == "synthetic transcript"
    assert adapter.calls["evt-1"] == 1


async def test_concurrent_duplicates_only_call_dependency_once(parts):
    processor, adapter, dlq, metrics = parts
    adapter.delay = 0.05

    outcomes = await asyncio.gather(*(processor.process(event()) for _ in range(5)))

    statuses = sorted(o.status.value for o in outcomes)
    assert statuses.count("processed") == 1
    assert statuses.count("in_progress") == 4
    assert adapter.calls["evt-1"] == 1


async def test_replay_resolves_dead_letters_once_dependency_recovers(parts):
    processor, adapter, dlq, metrics = parts
    adapter.failing = True
    for i in range(3):
        await processor.process(event(i))
    assert dlq.depth() == 3
    adapter.failing = False

    summary = await replay_dead_letters(processor, dlq)

    assert summary["resolved"] == 3 and summary["failed_again"] == 0
    assert dlq.depth() == 0
    assert all(r.status == "resolved" and r.resolved_by == "replay" for r in dlq.list())
    assert metrics.calls["replay:processed"] == 3


async def test_replaying_twice_does_not_process_twice(parts):
    processor, adapter, dlq, metrics = parts
    adapter.failing = True
    await processor.process(event())
    adapter.failing = False

    await replay_dead_letters(processor, dlq)
    second = await replay_dead_letters(processor, dlq)

    assert second["attempted"] == 0
    assert adapter.calls["evt-1"] == 3 + 1   # 3 failed live attempts + exactly 1 replay


async def test_concurrent_replays_process_each_event_once(parts):
    processor, adapter, dlq, metrics = parts
    adapter.failing = True
    for i in range(5):
        await processor.process(event(i))
    adapter.failing = False
    adapter.delay = 0.02

    a, b = await asyncio.gather(replay_dead_letters(processor, dlq), replay_dead_letters(processor, dlq))

    assert a["resolved"] + b["resolved"] == 5
    assert all(adapter.calls[f"evt-{i}"] == 3 + 1 for i in range(5))


async def test_replay_of_already_processed_event_marks_it_resolved_without_calling(parts):
    processor, adapter, dlq, metrics = parts
    adapter.failing = True
    await processor.process(event())
    adapter.failing = False
    await processor.process(event())          # client resubmitted and it succeeded
    calls_before = adapter.calls["evt-1"]

    summary = await replay_dead_letters(processor, dlq)

    assert dlq.get("evt-1").status == "resolved"
    assert summary["attempted"] == 0          # resubmission already resolved the row
    assert adapter.calls["evt-1"] == calls_before


async def test_failed_replay_keeps_event_pending(parts):
    processor, adapter, dlq, metrics = parts
    adapter.failing = True
    await processor.process(event())

    summary = await replay_dead_letters(processor, dlq)

    assert summary["failed_again"] == 1
    row = dlq.get("evt-1")
    assert row.status == "pending"
    assert row.replay_attempts == 1
    assert row.failure_count == 2


async def test_replay_stops_early_when_circuit_is_open(tmp_path):
    db = str(tmp_path / "svc.db")
    adapter = SwitchableAdapter(failing=True)
    metrics = Metrics()
    breaker = CircuitBreaker(window_size=4, min_calls=4, failure_rate_threshold=0.5, open_duration_s=60)
    client = ResilientPipelineClient(adapter, breaker, RetryPolicy(max_attempts=2, base_delay_s=0, max_delay_s=0, attempt_timeout_s=1),
                                     metrics=metrics, sleep=no_sleep)
    dlq = DeadLetterQueue(db)
    processor = EventProcessor(client, EventLedger(db), dlq, metrics)
    for i in range(5):
        await processor.process(event(i))     # breaker opens partway through

    summary = await replay_dead_letters(processor, dlq)

    assert summary["stopped_reason"] == "circuit_open"
    assert summary["attempted"] == 1
    assert dlq.depth() == 5


async def test_replay_specific_event_ids(parts):
    processor, adapter, dlq, metrics = parts
    adapter.failing = True
    for i in range(3):
        await processor.process(event(i))
    adapter.failing = False

    summary = await replay_dead_letters(processor, dlq, event_ids=["evt-2", "missing"])

    assert summary["resolved"] == 1
    assert summary["not_found"] == ["missing"]
    assert dlq.depth() == 2


async def test_dlq_write_failure_is_raised_not_swallowed(parts, monkeypatch):
    processor, adapter, dlq, metrics = parts
    adapter.failing = True

    def broken_add(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(dlq, "add", broken_add)

    with pytest.raises(OSError):
        await processor.process(event())
    # The claim was released so a later resubmission is not blocked.
    adapter.failing = False
    assert (await processor.process(event())).status is ProcessStatus.PROCESSED


async def test_in_flight_gauge_returns_to_zero(parts):
    processor, adapter, dlq, metrics = parts
    adapter.delay = 0.02

    await asyncio.gather(*(processor.process(event(i)) for i in range(10)))

    assert metrics.in_flight == 0
    assert metrics.max_in_flight_seen >= 2

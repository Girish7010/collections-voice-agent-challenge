"""Retry + timeout + circuit-breaker orchestration, against scripted fake adapters."""

import asyncio
import random

import pytest

from app.circuit_breaker import CircuitBreaker, CircuitState
from app.dependency_adapter import DependencyError, PipelineRequest, PipelineResult
from app.metrics import Metrics
from app.resilience import ResilientPipelineClient, RetryPolicy

REQ = PipelineRequest(event_id="evt-1")
OK = PipelineResult(transcript="synthetic transcript", response_audio_ref="synthetic", latency_ms=100)
RETRYABLE = DependencyError("downstream_timeout", retryable=True, status_code=503)
FATAL = DependencyError("http_400", retryable=False, status_code=400)


class ScriptedAdapter:
    """Plays back a list of outcomes: PipelineResult, Exception, or ("sleep", seconds)."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    async def process(self, request):
        self.calls += 1
        outcome = self.outcomes.pop(0) if self.outcomes else OK
        if isinstance(outcome, tuple) and outcome[0] == "sleep":
            await asyncio.sleep(outcome[1])
            return OK
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class FakeSleep:
    def __init__(self):
        self.calls: list[float] = []

    async def __call__(self, seconds):
        self.calls.append(seconds)


def make_client(adapter, *, breaker=None, metrics=None, sleep=None, **policy):
    return ResilientPipelineClient(
        adapter,
        breaker or CircuitBreaker(window_size=20, min_calls=10, failure_rate_threshold=0.5, open_duration_s=10),
        RetryPolicy(**{"max_attempts": 3, "base_delay_s": 0.1, "max_delay_s": 1.0, "attempt_timeout_s": 1.0, **policy}),
        sleep=sleep or FakeSleep(),
        rng=random.Random(1),
        metrics=metrics or Metrics(),
    )


async def test_success_on_first_attempt():
    adapter = ScriptedAdapter(OK)
    outcome = await make_client(adapter).process(REQ)

    assert outcome.ok and outcome.result == OK
    assert outcome.attempts == 1
    assert adapter.calls == 1


async def test_retry_then_success():
    adapter = ScriptedAdapter(RETRYABLE, RETRYABLE, OK)
    sleep = FakeSleep()
    metrics = Metrics()

    outcome = await make_client(adapter, sleep=sleep, metrics=metrics).process(REQ)

    assert outcome.ok
    assert outcome.attempts == 3
    assert len(sleep.calls) == 2          # one backoff before each retry
    snap = metrics.snapshot()
    assert snap["dependency"]["attempts"] == 3
    assert snap["dependency"]["failures"] == 2
    assert snap["dependency"]["retries"] == 2


async def test_exhausted_retries_report_last_reason():
    adapter = ScriptedAdapter(RETRYABLE, RETRYABLE, RETRYABLE, OK)

    outcome = await make_client(adapter).process(REQ)

    assert not outcome.ok
    assert outcome.attempts == 3
    assert outcome.failure_reason == "downstream_timeout"
    assert adapter.calls == 3              # bounded: never a 4th call


async def test_non_retryable_error_is_not_retried():
    adapter = ScriptedAdapter(FATAL, OK)

    outcome = await make_client(adapter).process(REQ)

    assert not outcome.ok
    assert outcome.attempts == 1
    assert outcome.failure_reason == "http_400"


async def test_unexpected_exception_becomes_failure_not_crash():
    adapter = ScriptedAdapter(RuntimeError("bug in adapter"))

    outcome = await make_client(adapter).process(REQ)

    assert not outcome.ok
    assert outcome.failure_reason == "internal_error:RuntimeError"


async def test_slow_attempt_times_out_and_is_retried():
    adapter = ScriptedAdapter(("sleep", 5), OK)

    outcome = await make_client(adapter, attempt_timeout_s=0.05).process(REQ)

    assert outcome.ok
    assert outcome.attempts == 2


async def test_all_attempts_timing_out_reports_timeout():
    adapter = ScriptedAdapter(("sleep", 5), ("sleep", 5))

    outcome = await make_client(adapter, attempt_timeout_s=0.02, max_attempts=2).process(REQ)

    assert not outcome.ok
    assert outcome.failure_reason == "timeout"


def test_backoff_is_exponential_capped_and_jittered():
    policy = RetryPolicy(max_attempts=6, base_delay_s=0.1, max_delay_s=0.5, attempt_timeout_s=1)
    rng = random.Random(0)

    for retry, ceiling in [(1, 0.1), (2, 0.2), (3, 0.4), (4, 0.5), (5, 0.5)]:
        samples = [policy.backoff(retry, rng) for _ in range(500)]
        assert all(0 <= s <= ceiling for s in samples)
        assert max(samples) > ceiling * 0.9   # uses the full range
        assert len(set(samples)) > 400        # jittered, not a fixed delay


async def test_open_circuit_fails_fast_without_calling_dependency():
    breaker = CircuitBreaker(window_size=4, min_calls=4, failure_rate_threshold=0.5, open_duration_s=60)
    for _ in range(4):
        breaker.record_failure()
    adapter = ScriptedAdapter(OK)
    metrics = Metrics()

    outcome = await make_client(adapter, breaker=breaker, metrics=metrics).process(REQ)

    assert not outcome.ok
    assert outcome.failure_reason == "circuit_open"
    assert outcome.attempts == 0
    assert adapter.calls == 0
    assert metrics.snapshot()["dependency"]["circuit_rejections"] == 1


async def test_failures_open_the_circuit_and_later_calls_fail_fast():
    breaker = CircuitBreaker(window_size=4, min_calls=4, failure_rate_threshold=0.5, open_duration_s=60)
    adapter = ScriptedAdapter(*[RETRYABLE] * 10)
    client = make_client(adapter, breaker=breaker, max_attempts=2)

    await client.process(REQ)   # 2 failures
    await client.process(REQ)   # 2 more -> 4/4 -> open
    calls_before = adapter.calls
    outcome = await client.process(REQ)

    assert breaker.state is CircuitState.OPEN
    assert outcome.failure_reason == "circuit_open"
    assert adapter.calls == calls_before


async def test_circuit_opening_mid_call_stops_further_retries():
    breaker = CircuitBreaker(window_size=2, min_calls=2, failure_rate_threshold=0.5, open_duration_s=60)
    breaker.record_failure()          # one failure already in the window
    adapter = ScriptedAdapter(RETRYABLE, OK)

    outcome = await make_client(adapter, breaker=breaker).process(REQ)

    assert adapter.calls == 1         # second attempt blocked by the now-open breaker
    assert outcome.failure_reason == "circuit_open"
    assert outcome.attempts == 1


async def test_half_open_probe_success_recovers():
    now = [0.0]
    breaker = CircuitBreaker(window_size=4, min_calls=4, failure_rate_threshold=0.5, open_duration_s=5, clock=lambda: now[0])
    for _ in range(4):
        breaker.record_failure()
    now[0] = 10.0
    metrics = Metrics()
    breaker.on_state_change = metrics.record_circuit_transition

    outcome = await make_client(ScriptedAdapter(OK), breaker=breaker, metrics=metrics).process(REQ)

    assert outcome.ok
    assert breaker.state is CircuitState.CLOSED
    names = [(t["from"], t["to"]) for t in metrics.snapshot()["circuit"]["transitions"]]
    assert names == [("open", "half_open"), ("half_open", "closed")]


async def test_non_retryable_errors_do_not_count_against_circuit():
    breaker = CircuitBreaker(window_size=4, min_calls=4, failure_rate_threshold=0.5, open_duration_s=60)
    client = make_client(ScriptedAdapter(*[FATAL] * 10), breaker=breaker)

    for _ in range(6):
        await client.process(REQ)

    assert breaker.state is CircuitState.CLOSED


@pytest.mark.parametrize("kw", [
    dict(max_attempts=0), dict(base_delay_s=-1), dict(max_delay_s=0.01, base_delay_s=0.1), dict(attempt_timeout_s=0),
])
def test_retry_policy_rejects_invalid_config(kw):
    base = dict(max_attempts=3, base_delay_s=0.1, max_delay_s=1.0, attempt_timeout_s=1.0)
    with pytest.raises(ValueError):
        RetryPolicy(**{**base, **kw})

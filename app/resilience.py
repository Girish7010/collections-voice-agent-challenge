"""Retries with exponential backoff + full jitter, per-attempt timeouts, and the
circuit breaker, wrapped around any PipelineAdapter.

How retry storms are avoided:
- attempts are bounded (`max_attempts`, default 3 = 1 try + 2 retries)
- backoff is exponential and fully jittered, so clients don't retry in lockstep
- the circuit breaker is checked before *every* attempt, including retries, so
  once the dependency looks unhealthy all callers stop sending traffic
- only retryable errors are retried and counted against dependency health
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.circuit_breaker import CircuitBreaker, CircuitState
from app.dependency_adapter import DependencyError, PipelineAdapter, PipelineRequest, PipelineResult
from app.metrics import Metrics

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay_s: float = 0.2
    max_delay_s: float = 2.0
    attempt_timeout_s: float = 2.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.base_delay_s < 0 or self.max_delay_s < self.base_delay_s:
            raise ValueError("need 0 <= base_delay_s <= max_delay_s")
        if self.attempt_timeout_s <= 0:
            raise ValueError("attempt_timeout_s must be > 0")

    def backoff(self, retry_number: int, rng: random.Random) -> float:
        """Full jitter: uniform(0, min(max_delay, base * 2^(n-1)))."""
        ceiling = min(self.max_delay_s, self.base_delay_s * 2 ** (retry_number - 1))
        return rng.uniform(0, ceiling)


@dataclass(frozen=True)
class CallOutcome:
    ok: bool
    attempts: int
    result: PipelineResult | None = None
    failure_reason: str | None = None


class ResilientPipelineClient:
    def __init__(
        self,
        adapter: PipelineAdapter,
        breaker: CircuitBreaker,
        policy: RetryPolicy,
        *,
        metrics: Metrics,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: random.Random | None = None,
    ):
        self.adapter = adapter
        self.breaker = breaker
        self.policy = policy
        self.metrics = metrics
        self._sleep = sleep
        self._rng = rng or random.Random()

    async def process(self, request: PipelineRequest) -> CallOutcome:
        attempts = 0
        last_reason: str | None = None

        while attempts < self.policy.max_attempts:
            if not self.breaker.allow_request():
                self.metrics.record_circuit_rejection()
                return CallOutcome(ok=False, attempts=attempts, failure_reason="circuit_open")

            attempts += 1
            if attempts > 1:
                self.metrics.record_retry()
            self.metrics.record_attempt()

            try:
                result = await asyncio.wait_for(self.adapter.process(request), self.policy.attempt_timeout_s)
            except TimeoutError:
                error = DependencyError("timeout", retryable=True)
            except DependencyError as exc:
                error = exc
            except Exception as exc:  # adapter bug: never lose the event, dead-letter it instead
                logger.exception("unexpected adapter error event_id=%s", request.event_id)
                error = DependencyError(f"internal_error:{type(exc).__name__}", retryable=False)
            else:
                self.breaker.record_success()
                return CallOutcome(ok=True, attempts=attempts, result=result)

            last_reason = error.reason
            self.metrics.record_attempt_failure(error.reason)
            if not error.retryable:
                self.breaker.release()
                return CallOutcome(ok=False, attempts=attempts, failure_reason=error.reason)

            self.breaker.record_failure()
            if attempts < self.policy.max_attempts and self.breaker.state is not CircuitState.OPEN:
                await self._sleep(self.policy.backoff(attempts, self._rng))

        return CallOutcome(ok=False, attempts=attempts, failure_reason=last_reason)

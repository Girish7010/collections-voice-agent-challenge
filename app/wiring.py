"""Builds the object graph from Settings. Shared by the API and the CLI."""

from __future__ import annotations

from dataclasses import dataclass

from app.circuit_breaker import CircuitBreaker
from app.config import Settings
from app.dependency_adapter import HttpPipelineAdapter, PipelineAdapter
from app.dlq import DeadLetterQueue
from app.event_processor import EventProcessor
from app.idempotency import EventLedger
from app.metrics import Metrics
from app.resilience import ResilientPipelineClient, RetryPolicy


@dataclass
class Components:
    settings: Settings
    adapter: PipelineAdapter
    breaker: CircuitBreaker
    metrics: Metrics
    dlq: DeadLetterQueue
    processor: EventProcessor

    async def aclose(self) -> None:
        close = getattr(self.adapter, "aclose", None)
        if close:
            await close()


def build(settings: Settings, adapter: PipelineAdapter | None = None) -> Components:
    metrics = Metrics()
    breaker = CircuitBreaker(
        failure_rate_threshold=settings.cb_failure_threshold,
        window_size=settings.cb_window,
        min_calls=settings.cb_min_calls,
        open_duration_s=settings.cb_open_s,
        half_open_max_calls=settings.cb_half_open_calls,
        on_state_change=metrics.record_circuit_transition,
    )
    policy = RetryPolicy(
        max_attempts=settings.max_attempts,
        base_delay_s=settings.base_delay_s,
        max_delay_s=settings.max_delay_s,
        attempt_timeout_s=settings.attempt_timeout_s,
    )
    # The HTTP timeout is a backstop; the per-attempt deadline is enforced by RetryPolicy.
    adapter = adapter or HttpPipelineAdapter(settings.dependency_url, timeout_s=settings.attempt_timeout_s + 1)
    client = ResilientPipelineClient(adapter, breaker, policy, metrics=metrics)
    dlq = DeadLetterQueue(settings.db_path)
    processor = EventProcessor(client, EventLedger(settings.db_path), dlq, metrics,
                               max_concurrency=settings.max_concurrency)
    return Components(settings, adapter, breaker, metrics, dlq, processor)

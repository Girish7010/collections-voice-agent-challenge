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
from app.stores import DeadLetterStore, LedgerStore


@dataclass
class Components:
    settings: Settings
    adapter: PipelineAdapter
    breaker: CircuitBreaker
    metrics: Metrics
    dlq: DeadLetterStore
    processor: EventProcessor

    async def aclose(self) -> None:
        close = getattr(self.adapter, "aclose", None)
        if close:
            await close()
        self.processor.close()


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
    adapter = adapter or HttpPipelineAdapter(settings.dependency_url, timeout_s=settings.attempt_timeout_s + 1,
                                             max_connections=settings.max_concurrency)
    client = ResilientPipelineClient(adapter, breaker, policy, metrics=metrics)
    dlq, ledger = build_stores(settings)
    processor = EventProcessor(client, ledger, dlq, metrics,
                               max_concurrency=settings.max_concurrency,
                               db_threads=settings.db_threads)
    return Components(settings, adapter, breaker, metrics, dlq, processor)


def build_stores(settings: Settings) -> tuple[DeadLetterStore, LedgerStore]:
    """The dead-letter queue and ledger for the configured backend.

    The DynamoDB modules are imported lazily so the SQLite path never needs boto3,
    which keeps the local quick start in README.md working without AWS installed.
    """
    if settings.store_backend == "dynamodb":
        from app.dynamo_dlq import DynamoDeadLetterQueue
        from app.dynamo_idempotency import DynamoEventLedger

        region = settings.aws_region or None
        return (
            DynamoDeadLetterQueue(settings.dlq_table, region=region),
            DynamoEventLedger(settings.ledger_table, region=region,
                              ttl_days=settings.ledger_ttl_days),
        )
    return DeadLetterQueue(settings.db_path), EventLedger(settings.db_path)

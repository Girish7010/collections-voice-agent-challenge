"""Service settings, read from APP_* environment variables (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass, fields

STORE_BACKENDS = ("sqlite", "dynamodb")


@dataclass(frozen=True)
class Settings:
    dependency_url: str = "http://localhost:8001"
    # storage: where the dead-letter queue and the idempotency ledger live.
    #   "sqlite"   one file per process. Correct only for a single task, because
    #              nothing is shared and the file dies with the task.
    #   "dynamodb" shared across tasks; required for more than one replica.
    store_backend: str = "sqlite"
    db_path: str = "data/collections.db"          # sqlite only
    dlq_table: str = ""                           # dynamodb only
    ledger_table: str = ""                        # dynamodb only
    aws_region: str = ""                          # dynamodb only; else the boto3 default chain
    # How long a succeeded ledger record is kept. Past this, a duplicate of the same
    # event_id is treated as new and processed again. 0 keeps records forever.
    ledger_ttl_days: int = 30
    # Store calls block, so they run on a worker pool. SQLite needs exactly one
    # thread; DynamoDB calls are network round-trips, so serializing them on one
    # thread caps throughput - raise this when store_backend is "dynamodb".
    db_threads: int = 1
    # retries / timeouts
    max_attempts: int = 3
    base_delay_s: float = 0.2
    max_delay_s: float = 2.0
    attempt_timeout_s: float = 2.0
    # circuit breaker
    cb_failure_threshold: float = 0.5
    cb_window: int = 50
    cb_min_calls: int = 20
    cb_open_s: float = 10.0
    cb_half_open_calls: int = 1
    # bulkhead: max concurrent calls to the dependency from this process
    max_concurrency: int = 200
    log_level: str = "INFO"

    def __post_init__(self) -> None:
        if self.store_backend not in STORE_BACKENDS:
            raise ValueError(
                f"APP_STORE_BACKEND={self.store_backend!r} is not one of {sorted(STORE_BACKENDS)}"
            )
        if self.store_backend == "dynamodb":
            missing = [name for name, value in
                       (("APP_DLQ_TABLE", self.dlq_table), ("APP_LEDGER_TABLE", self.ledger_table))
                       if not value]
            if missing:
                raise ValueError(
                    f"store_backend='dynamodb' requires {' and '.join(missing)} to be set"
                )
        if self.db_threads < 1:
            raise ValueError(f"APP_DB_THREADS={self.db_threads} must be at least 1")
        # Each SQLite call opens its own short-lived connection, but schema creation
        # and the WAL pragma race if several threads start at once.
        if self.store_backend == "sqlite" and self.db_threads != 1:
            raise ValueError("store_backend='sqlite' requires APP_DB_THREADS=1")

    @classmethod
    def from_env(cls) -> Settings:
        values = {}
        for f in fields(cls):
            env = f"APP_{f.name.upper()}"
            raw = os.getenv(env)
            if raw is None or raw == "":
                continue
            cast = {"int": int, "float": float}.get(f.type, str)
            try:
                values[f.name] = cast(raw)
            except ValueError as exc:
                raise ValueError(f"{env}={raw!r} is not a valid {f.type}") from exc
        return cls(**values)

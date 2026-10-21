"""Service settings, read from APP_* environment variables (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass, fields


@dataclass(frozen=True)
class Settings:
    dependency_url: str = "http://localhost:8001"
    db_path: str = "data/collections.db"
    # retries / timeouts
    max_attempts: int = 3
    base_delay_s: float = 0.2
    max_delay_s: float = 2.0
    attempt_timeout_s: float = 2.0
    # circuit breaker
    cb_failure_threshold: float = 0.5
    cb_window: int = 20
    cb_min_calls: int = 10
    cb_open_s: float = 10.0
    cb_half_open_calls: int = 1
    # bulkhead: max concurrent calls to the dependency from this process
    max_concurrency: int = 200
    log_level: str = "INFO"

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

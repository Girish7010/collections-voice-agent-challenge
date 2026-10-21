"""In-process metrics, exposed as JSON on GET /metrics.

Two failure rates are kept apart on purpose:
- dependency failure rate: failed attempts / all attempts to the pipeline
  (what the downstream does to us, ~20% with the mock)
- final call failure rate: calls dead-lettered / calls finished
  (what is left after retries)
"""

from __future__ import annotations

import time
from collections import Counter, deque
from datetime import datetime, timezone
from enum import Enum

LATENCY_SAMPLES = 10_000
TRANSITIONS_KEPT = 200


def _pct(sorted_values: list[float], p: float) -> float | None:
    if not sorted_values:
        return None
    return round(sorted_values[min(len(sorted_values) - 1, int(p / 100 * len(sorted_values)))], 1)


class Metrics:
    def __init__(self) -> None:
        self.started_at = time.time()
        self.calls: Counter[str] = Counter()
        self.dependency: Counter[str] = Counter()
        self.failure_reasons: Counter[str] = Counter()
        self.in_flight = 0
        self.max_in_flight_seen = 0
        self._latencies_ms: deque[float] = deque(maxlen=LATENCY_SAMPLES)
        self._transitions: deque[dict] = deque(maxlen=TRANSITIONS_KEPT)

    # --- dependency / resilience layer ---
    def record_attempt(self) -> None:
        self.dependency["attempts"] += 1

    def record_attempt_failure(self, reason: str) -> None:
        self.dependency["failures"] += 1
        self.failure_reasons[reason] += 1

    def record_retry(self) -> None:
        self.dependency["retries"] += 1

    def record_circuit_rejection(self) -> None:
        self.dependency["circuit_rejections"] += 1

    def record_circuit_transition(self, old: Enum, new: Enum) -> None:
        self._transitions.append({
            "at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "from": getattr(old, "value", str(old)),
            "to": getattr(new, "value", str(new)),
        })

    # --- call level ---
    def record_call(self, outcome: str, source: str, latency_ms: float | None = None) -> None:
        self.calls[f"{source}:{outcome}"] += 1
        if latency_ms is not None and outcome == "processed":
            self._latencies_ms.append(latency_ms)

    def call_started(self) -> None:
        self.in_flight += 1
        self.max_in_flight_seen = max(self.max_in_flight_seen, self.in_flight)

    def call_finished(self) -> None:
        self.in_flight -= 1

    def snapshot(self, *, circuit_state: str | None = None, dlq: dict | None = None) -> dict:
        attempts = self.dependency["attempts"]
        live_ok = self.calls["live:processed"]
        live_dl = self.calls["live:dead_lettered"]
        lat = sorted(self._latencies_ms)
        uptime = max(time.time() - self.started_at, 1e-9)
        finished = sum(v for k, v in self.calls.items() if k.endswith((":processed", ":dead_lettered")))
        return {
            "uptime_s": round(uptime, 1),
            "in_flight": self.in_flight,
            "max_in_flight_seen": self.max_in_flight_seen,
            "calls": dict(sorted(self.calls.items())),
            "calls_per_s": round(finished / uptime, 3),
            "dependency": {
                "attempts": attempts,
                "failures": self.dependency["failures"],
                "retries": self.dependency["retries"],
                "circuit_rejections": self.dependency["circuit_rejections"],
                "failure_rate": round(self.dependency["failures"] / attempts, 4) if attempts else None,
                "failures_by_reason": dict(self.failure_reasons),
            },
            "final_call_failure_rate": round(live_dl / (live_ok + live_dl), 4) if (live_ok + live_dl) else None,
            "latency_ms": {"count": len(lat), "p50": _pct(lat, 50), "p90": _pct(lat, 90), "p99": _pct(lat, 99),
                           "max": round(lat[-1], 1) if lat else None},
            "circuit": {"state": circuit_state, "transitions": list(self._transitions)},
            "dlq": dlq or {},
        }

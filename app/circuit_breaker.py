"""Count-based sliding-window circuit breaker.

CLOSED     Requests flow. The last `window_size` outcomes are kept; once at least
           `min_calls` are recorded and the failure share reaches
           `failure_rate_threshold`, the breaker OPENS.
OPEN       Requests are rejected immediately (fail fast, no load on a sick
           dependency). After `open_duration_s` it becomes HALF_OPEN.
HALF_OPEN  Up to `half_open_max_calls` probe requests are let through. A probe
           success CLOSES the breaker (with a fresh window); a probe failure
           re-OPENS it and restarts the timer.

Why a failure *rate* over a window and not "N consecutive failures": the mock
fails ~20% of requests independently. Under concurrency, short runs of
consecutive failures happen by chance and would trip a consecutive-count
breaker during normal operation. A 50% threshold over 20 calls tolerates the
normal 20% but trips quickly on a real outage.

Single-threaded asyncio use only: no method awaits, so no locking is needed.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from enum import Enum


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


StateChangeFn = Callable[[CircuitState, CircuitState], None]


class CircuitBreaker:
    def __init__(
        self,
        *,
        failure_rate_threshold: float = 0.5,
        window_size: int = 20,
        min_calls: int = 10,
        open_duration_s: float = 10.0,
        half_open_max_calls: int = 1,
        clock: Callable[[], float] = time.monotonic,
        on_state_change: StateChangeFn | None = None,
    ):
        if not 0 < failure_rate_threshold <= 1:
            raise ValueError("failure_rate_threshold must be in (0, 1]")
        if window_size < 1 or not 1 <= min_calls <= window_size:
            raise ValueError("need window_size >= 1 and 1 <= min_calls <= window_size")
        if open_duration_s < 0:
            raise ValueError("open_duration_s must be >= 0")
        if half_open_max_calls < 1:
            raise ValueError("half_open_max_calls must be >= 1")

        self.failure_rate_threshold = failure_rate_threshold
        self.min_calls = min_calls
        self.open_duration_s = open_duration_s
        self.half_open_max_calls = half_open_max_calls
        self.on_state_change = on_state_change
        self._clock = clock
        self._window: deque[bool] = deque(maxlen=window_size)  # True = failure
        self._state = CircuitState.CLOSED
        self._opened_at = 0.0
        self._half_open_in_flight = 0

    @property
    def state(self) -> CircuitState:
        if self._state is CircuitState.OPEN and self._clock() - self._opened_at >= self.open_duration_s:
            self._transition(CircuitState.HALF_OPEN)
        return self._state

    def allow_request(self) -> bool:
        state = self.state
        if state is CircuitState.CLOSED:
            return True
        if state is CircuitState.HALF_OPEN and self._half_open_in_flight < self.half_open_max_calls:
            self._half_open_in_flight += 1
            return True
        return False

    def record_success(self) -> None:
        state = self.state
        if state is CircuitState.HALF_OPEN:
            self._transition(CircuitState.CLOSED)
        elif state is CircuitState.CLOSED:
            self._window.append(False)

    def record_failure(self) -> None:
        state = self.state
        if state is CircuitState.HALF_OPEN:
            self._transition(CircuitState.OPEN)
        elif state is CircuitState.CLOSED:
            self._window.append(True)
            if len(self._window) >= self.min_calls and self.failure_rate() >= self.failure_rate_threshold:
                self._transition(CircuitState.OPEN)

    def release(self) -> None:
        """A permitted call ended without a verdict on dependency health."""
        if self._state is CircuitState.HALF_OPEN and self._half_open_in_flight > 0:
            self._half_open_in_flight -= 1

    def failure_rate(self) -> float:
        return sum(self._window) / len(self._window) if self._window else 0.0

    def _transition(self, new: CircuitState) -> None:
        old = self._state
        self._state = new
        self._half_open_in_flight = 0
        if new is CircuitState.OPEN:
            self._opened_at = self._clock()
        elif new is CircuitState.CLOSED:
            self._window.clear()
        if self.on_state_change:
            self.on_state_change(old, new)

"""Circuit breaker: closed -> open -> half-open -> closed/open, driven by a fake clock."""

import pytest

from app.circuit_breaker import CircuitBreaker, CircuitState


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_breaker(clock=None, transitions=None, **kw) -> CircuitBreaker:
    defaults = dict(failure_rate_threshold=0.5, window_size=10, min_calls=4, open_duration_s=5.0, half_open_max_calls=1)
    defaults.update(kw)
    on_change = (lambda old, new: transitions.append((old, new))) if transitions is not None else None
    return CircuitBreaker(clock=clock or FakeClock(), on_state_change=on_change, **defaults)


def test_starts_closed_and_allows_requests():
    cb = make_breaker()
    assert cb.state is CircuitState.CLOSED
    assert cb.allow_request() is True


def test_does_not_open_before_min_calls():
    cb = make_breaker(min_calls=4)
    for _ in range(3):
        cb.record_failure()
    assert cb.state is CircuitState.CLOSED


def test_opens_when_failure_rate_reaches_threshold():
    transitions = []
    cb = make_breaker(transitions=transitions)
    cb.record_success()
    cb.record_success()
    cb.record_failure()
    assert cb.state is CircuitState.CLOSED  # 1/3 calls, below min_calls
    cb.record_failure()                     # 2/4 = 50% -> open

    assert cb.state is CircuitState.OPEN
    assert transitions == [(CircuitState.CLOSED, CircuitState.OPEN)]


def test_normal_20_percent_failures_do_not_open():
    cb = make_breaker(window_size=20, min_calls=10)
    for i in range(200):
        cb.record_failure() if i % 5 == 0 else cb.record_success()
    assert cb.state is CircuitState.CLOSED


def test_window_only_counts_recent_calls():
    cb = make_breaker(window_size=4, min_calls=4)
    cb.record_failure()
    for _ in range(4):
        cb.record_success()    # pushes the old failure out of the window
    cb.record_failure()
    assert cb.state is CircuitState.CLOSED  # window = S,S,S,F -> 25%


def test_open_rejects_requests_fast():
    cb = make_breaker()
    for _ in range(4):
        cb.record_failure()
    assert cb.allow_request() is False


def test_moves_to_half_open_after_open_duration():
    clock = FakeClock()
    cb = make_breaker(clock=clock, open_duration_s=5.0)
    for _ in range(4):
        cb.record_failure()

    clock.advance(4.9)
    assert cb.state is CircuitState.OPEN
    clock.advance(0.2)
    assert cb.state is CircuitState.HALF_OPEN


def test_half_open_limits_probe_calls():
    clock = FakeClock()
    cb = make_breaker(clock=clock, half_open_max_calls=1)
    for _ in range(4):
        cb.record_failure()
    clock.advance(10)

    assert cb.allow_request() is True    # the single probe
    assert cb.allow_request() is False   # everyone else still fails fast


def test_half_open_success_closes_and_resets_window():
    clock = FakeClock()
    transitions = []
    cb = make_breaker(clock=clock, transitions=transitions)
    for _ in range(4):
        cb.record_failure()
    clock.advance(10)
    cb.allow_request()

    cb.record_success()

    assert cb.state is CircuitState.CLOSED
    assert transitions == [
        (CircuitState.CLOSED, CircuitState.OPEN),
        (CircuitState.OPEN, CircuitState.HALF_OPEN),
        (CircuitState.HALF_OPEN, CircuitState.CLOSED),
    ]
    cb.record_failure()                  # old failures were forgotten
    assert cb.state is CircuitState.CLOSED


def test_half_open_failure_reopens_and_restarts_timer():
    clock = FakeClock()
    cb = make_breaker(clock=clock, open_duration_s=5.0)
    for _ in range(4):
        cb.record_failure()
    clock.advance(10)
    cb.allow_request()

    cb.record_failure()

    assert cb.state is CircuitState.OPEN
    clock.advance(4.9)
    assert cb.state is CircuitState.OPEN


def test_release_frees_a_half_open_probe_slot():
    clock = FakeClock()
    cb = make_breaker(clock=clock, half_open_max_calls=1)
    for _ in range(4):
        cb.record_failure()
    clock.advance(10)
    assert cb.allow_request() is True

    cb.release()   # probe ended with a non-dependency error: no verdict

    assert cb.state is CircuitState.HALF_OPEN
    assert cb.allow_request() is True


@pytest.mark.parametrize("kw", [
    dict(failure_rate_threshold=0), dict(failure_rate_threshold=1.5),
    dict(window_size=0), dict(min_calls=0), dict(min_calls=50, window_size=10),
    dict(open_duration_s=-1), dict(half_open_max_calls=0),
])
def test_rejects_invalid_config(kw):
    with pytest.raises(ValueError):
        make_breaker(**kw)

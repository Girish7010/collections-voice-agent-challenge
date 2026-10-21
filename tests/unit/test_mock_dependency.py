"""Unit tests for the Appendix B mock STT -> LLM -> TTS dependency.

Randomness and sleeping are injected, so these tests are deterministic and fast:
- failure_rate=0.0 / 1.0 forces success / failure
- a seeded random.Random makes statistical checks reproducible
- a fake async sleep records the simulated latency instead of waiting for it
"""

import random

import pytest
from fastapi.testclient import TestClient

from mock_dependency.api import MockConfig, create_app, sample_latency_ms

VALID_BODY = {"event_id": "a1f2c3d4-0001", "audio_ref": "synthetic"}


class FakeSleep:
    """Records requested sleep durations (seconds) without actually sleeping."""

    def __init__(self):
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def make_client(failure_rate: float, seed: int = 42, sleep=None, **cfg) -> TestClient:
    config = MockConfig(failure_rate=failure_rate, **cfg)
    app = create_app(config, rng=random.Random(seed), sleep=sleep or FakeSleep())
    return TestClient(app)


def percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(p / 100 * len(ordered)))]


def test_success_response_matches_contract():
    client = make_client(failure_rate=0.0)

    resp = client.post("/pipeline/process", json=VALID_BODY)

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["transcript"] == "synthetic transcript"
    assert body["response_audio_ref"] == "synthetic"
    assert isinstance(body["latency_ms"], int)
    assert body["latency_ms"] > 0
    assert set(body) == {"status", "transcript", "response_audio_ref", "latency_ms"}


def test_failure_returns_503_with_exact_body():
    client = make_client(failure_rate=1.0)

    resp = client.post("/pipeline/process", json=VALID_BODY)

    assert resp.status_code == 503
    assert resp.json() == {"status": "error", "reason": "downstream_timeout"}


def test_success_sleeps_for_the_reported_latency():
    sleep = FakeSleep()
    client = make_client(failure_rate=0.0, sleep=sleep)

    body = client.post("/pipeline/process", json=VALID_BODY).json()

    assert len(sleep.calls) == 1
    assert sleep.calls[0] == pytest.approx(body["latency_ms"] / 1000, abs=0.001)


def test_failure_does_not_sleep_by_default():
    sleep = FakeSleep()
    client = make_client(failure_rate=1.0, sleep=sleep)

    client.post("/pipeline/process", json=VALID_BODY)

    assert sleep.calls == []


def test_failure_latency_is_configurable():
    sleep = FakeSleep()
    client = make_client(failure_rate=1.0, sleep=sleep, failure_latency_ms=250)

    client.post("/pipeline/process", json=VALID_BODY)

    assert sleep.calls == [pytest.approx(0.25)]


def test_failure_rate_is_about_20_percent_by_default():
    client = make_client(failure_rate=MockConfig().failure_rate, seed=7)
    n = 2000

    failures = sum(
        client.post("/pipeline/process", json={**VALID_BODY, "event_id": f"evt-{i}"}).status_code == 503
        for i in range(n)
    )

    # 20% of 2000 = 400; binomial std dev ~ 17.9, so +/- 60 is ~3.3 sigma.
    assert 340 <= failures <= 460


def test_failures_are_independent_per_request_not_bursty():
    """Longest run of consecutive failures should look like independent coin flips."""
    client = make_client(failure_rate=0.2, seed=11)
    statuses = [client.post("/pipeline/process", json=VALID_BODY).status_code for _ in range(2000)]

    longest = current = 0
    for status in statuses:
        current = current + 1 if status == 503 else 0
        longest = max(longest, current)

    # With p=0.2 and n=2000, a run of 9+ failures is very unlikely (~0.5%).
    assert longest < 9


def test_same_seed_gives_same_sequence():
    a = make_client(failure_rate=0.2, seed=123)
    b = make_client(failure_rate=0.2, seed=123)

    seq_a = [a.post("/pipeline/process", json=VALID_BODY).json() for _ in range(50)]
    seq_b = [b.post("/pipeline/process", json=VALID_BODY).json() for _ in range(50)]

    assert seq_a == seq_b


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"audio_ref": "synthetic"},
        {"event_id": "", "audio_ref": "synthetic"},
        {"event_id": 123, "audio_ref": "synthetic"},
    ],
)
def test_invalid_request_is_rejected_with_422(body):
    client = make_client(failure_rate=0.0)

    resp = client.post("/pipeline/process", json=body)

    assert resp.status_code == 422


def test_audio_ref_defaults_to_synthetic():
    client = make_client(failure_rate=0.0)

    resp = client.post("/pipeline/process", json={"event_id": "evt-1"})

    assert resp.status_code == 200


def test_health_endpoint_reports_config():
    client = make_client(failure_rate=0.2)

    resp = client.get("/health")

    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert resp.json()["failure_rate"] == 0.2


def test_default_latency_distribution_hits_appendix_b_targets():
    """p50 ~ 300ms, p90 ~ 700ms, p99 ~ 1200ms (Appendix B), within ~10-15%."""
    cfg = MockConfig()
    rng = random.Random(2026)
    samples = [sample_latency_ms(rng, cfg.latency_mu, cfg.latency_sigma) for _ in range(50_000)]

    assert 270 <= percentile(samples, 50) <= 330
    assert 600 <= percentile(samples, 90) <= 770
    assert 1050 <= percentile(samples, 99) <= 1350


def test_config_reads_environment(monkeypatch):
    monkeypatch.setenv("MOCK_FAILURE_RATE", "0.35")
    monkeypatch.setenv("MOCK_LATENCY_MU", "5.0")
    monkeypatch.setenv("MOCK_LATENCY_SIGMA", "0.4")
    monkeypatch.setenv("MOCK_FAILURE_LATENCY_MS", "100")
    monkeypatch.setenv("MOCK_SEED", "9")

    cfg = MockConfig.from_env()

    assert cfg == MockConfig(
        failure_rate=0.35, latency_mu=5.0, latency_sigma=0.4, failure_latency_ms=100, seed=9
    )


@pytest.mark.parametrize("rate", [-0.1, 1.5])
def test_config_rejects_out_of_range_failure_rate(rate):
    with pytest.raises(ValueError):
        MockConfig(failure_rate=rate)

"""HTTP API of the inference service, with a fake in-process adapter."""

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.config import Settings
from app.dependency_adapter import DependencyError, PipelineResult

OK = PipelineResult(transcript="synthetic transcript", response_audio_ref="synthetic", latency_ms=42)


class SwitchableAdapter:
    def __init__(self):
        self.failing = False
        self.calls = 0

    async def process(self, request):
        self.calls += 1
        if self.failing:
            raise DependencyError("downstream_timeout", retryable=True, status_code=503)
        return OK


def event(i=1):
    return {"event_id": f"evt-{i}", "campaign_id": "CAMP-2026-09-A", "borrower_bucket": "0-30 DPD",
            "product_type": "bnpl", "timestamp": "2026-09-01T09:03:12Z", "outcome": "connected"}


def make(tmp_path, *, raise_server_exceptions=True, **overrides):
    settings = Settings(**{"db_path": str(tmp_path / "api.db"), "base_delay_s": 0, "max_delay_s": 0,
                           "cb_window": 4, "cb_min_calls": 4, "cb_open_s": 60, **overrides})
    adapter = SwitchableAdapter()
    app = create_app(settings, adapter=adapter)
    return TestClient(app, raise_server_exceptions=raise_server_exceptions), adapter


@pytest.fixture
def setup(tmp_path):
    client, adapter = make(tmp_path)
    with client:
        yield client, adapter


@pytest.fixture
def tolerant(tmp_path):
    """Breaker that will not open during a handful of failures."""
    client, adapter = make(tmp_path, cb_window=20, cb_min_calls=20)
    with client:
        yield client, adapter


def test_health(setup):
    client, _ = setup
    body = client.get("/health").json()
    assert body == {"status": "ok", "circuit_state": "closed"}


def test_successful_call_returns_200_processed(setup):
    client, _ = setup
    resp = client.post("/calls", json=event())

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "processed"
    assert body["result"]["transcript"] == "synthetic transcript"


def test_duplicate_call_returns_200_duplicate(setup):
    client, adapter = setup
    client.post("/calls", json=event())

    resp = client.post("/calls", json=event())

    assert resp.status_code == 200
    assert resp.json()["status"] == "duplicate"
    assert adapter.calls == 1


def test_failed_call_returns_202_dead_lettered_and_is_inspectable(setup):
    client, adapter = setup
    adapter.failing = True

    resp = client.post("/calls", json=event())

    assert resp.status_code == 202
    assert resp.json()["status"] == "dead_lettered"
    assert resp.json()["failure_reason"] == "downstream_timeout"

    listing = client.get("/dlq").json()
    assert listing["counts"] == {"pending": 1}
    assert listing["items"][0]["event_id"] == "evt-1"
    one = client.get("/dlq/evt-1").json()
    assert one["retry_count"] == 2 and one["status"] == "pending"


def test_unknown_dlq_entry_is_404(setup):
    client, _ = setup
    assert client.get("/dlq/nope").status_code == 404


def test_dlq_write_failure_returns_500_and_never_claims_the_call_was_stored(tmp_path):
    """If the dead letter itself cannot be written, the sender must be told.

    A 202 here would be a lie: it promises the call is safely queued for replay
    when nothing was persisted, which is exactly the silent loss this service
    exists to prevent.
    """
    client, adapter = make(tmp_path, raise_server_exceptions=False)
    with client:
        adapter.failing = True

        def broken_add(*args, **kwargs):
            raise OSError("disk full")

        client.app.state.components.dlq.add = broken_add

        resp = client.post("/calls", json=event())

        assert resp.status_code == 500


def test_replay_endpoint_resolves_dead_letters(setup):
    client, adapter = setup
    adapter.failing = True
    client.post("/calls", json=event(1))
    adapter.failing = False

    summary = client.post("/dlq/replay", json={}).json()

    assert summary["resolved"] == 1
    assert client.get("/dlq", params={"status": "pending"}).json()["items"] == []
    assert client.get("/dlq/evt-1").json()["resolved_by"] == "replay"


def test_replay_specific_ids(tolerant):
    client, adapter = tolerant
    adapter.failing = True
    client.post("/calls", json=event(1))
    client.post("/calls", json=event(2))
    adapter.failing = False

    summary = client.post("/dlq/replay", json={"event_ids": ["evt-2"]}).json()

    assert summary["resolved"] == 1
    assert client.get("/dlq/evt-1").json()["status"] == "pending"


def test_invalid_event_is_422(setup):
    client, _ = setup
    assert client.post("/calls", json={"event_id": "x"}).status_code == 422


def test_metrics_separate_dependency_and_final_failure_rates(setup):
    client, adapter = setup
    client.post("/calls", json=event(1))
    adapter.failing = True
    client.post("/calls", json=event(2))

    m = client.get("/metrics").json()

    assert m["calls"]["live:processed"] == 1
    assert m["calls"]["live:dead_lettered"] == 1
    assert m["dependency"]["attempts"] == 4 or m["dependency"]["attempts"] == 2  # breaker may cut retries
    assert m["dependency"]["failure_rate"] > 0
    assert m["final_call_failure_rate"] == 0.5
    assert m["dlq"]["depth"] == 1
    assert m["circuit"]["state"] in {"closed", "open"}
    assert m["in_flight"] == 0


def test_circuit_state_visible_in_health_after_outage(setup):
    client, adapter = setup
    adapter.failing = True
    for i in range(3):
        client.post("/calls", json=event(i))

    assert client.get("/health").json()["circuit_state"] == "open"
    m = client.get("/metrics").json()
    assert [t["to"] for t in m["circuit"]["transitions"]] == ["open"]

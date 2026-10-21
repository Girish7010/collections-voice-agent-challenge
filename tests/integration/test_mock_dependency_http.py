"""Integration test: run the mock under a real uvicorn server and call it over HTTP.

Unlike the unit tests, this uses real sockets and real sleeping, so it checks
that the simulated latency is actually observed by a network client.
"""

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def mock_url():
    port = free_port()
    env = {**os.environ, "MOCK_SEED": "2026", "MOCK_FAILURE_RATE": "0.2"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "mock_dependency.api:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=REPO_ROOT, env=env,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 15
        while True:
            try:
                if httpx.get(f"{url}/health", timeout=1).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            if time.monotonic() > deadline or proc.poll() is not None:
                raise RuntimeError("mock dependency did not start")
            time.sleep(0.1)
        yield url
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_real_http_calls_follow_contract_and_latency(mock_url):
    statuses = []
    with httpx.Client(base_url=mock_url, timeout=10) as client:
        for i in range(40):
            started = time.perf_counter()
            resp = client.post("/pipeline/process", json={"event_id": f"it-{i}", "audio_ref": "synthetic"})
            elapsed_ms = (time.perf_counter() - started) * 1000
            statuses.append(resp.status_code)

            if resp.status_code == 200:
                body = resp.json()
                assert body["status"] == "ok"
                # Client-observed time must cover the simulated latency.
                assert elapsed_ms >= body["latency_ms"] - 5
            else:
                assert resp.status_code == 503
                assert resp.json() == {"status": "error", "reason": "downstream_timeout"}

    # Both outcomes appear in a modest sample (seeded, so deterministic).
    assert 200 in statuses and 503 in statuses

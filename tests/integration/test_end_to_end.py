"""End to end: real mock + real service (uvicorn subprocesses) + traffic generator.

These are the "reproducible load tests": seeded, short (compressed time), and
asserting the properties that matter: no call lost, retries bring the final
failure rate far below the dependency failure rate, an outage opens the
circuit, and replay drains the DLQ afterwards.
"""

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from load_tests.traffic_generator import LoadConfig, run_load

pytestmark = pytest.mark.integration
REPO = Path(__file__).resolve().parents[2]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start(args, env, url):
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", *args, "--host", "127.0.0.1", "--log-level", "warning"],
                            cwd=REPO, env={**os.environ, **env},
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{url}/health", timeout=1).status_code == 200:
                return proc
        except httpx.TransportError:
            time.sleep(0.1)
    proc.kill()
    raise RuntimeError(f"{args[0]} did not start")


@pytest.fixture
def stack(tmp_path):
    mport, sport = free_port(), free_port()
    mock_url, svc_url = f"http://127.0.0.1:{mport}", f"http://127.0.0.1:{sport}"
    procs = [start(["mock_dependency.api:app", "--port", str(mport)], {"MOCK_SEED": "2026"}, mock_url)]
    procs.append(start(["app.api:app_from_env", "--factory", "--port", str(sport)], {
        "APP_DEPENDENCY_URL": mock_url, "APP_DB_PATH": str(tmp_path / "e2e.db"),
        "APP_CB_OPEN_S": "1", "APP_LOG_LEVEL": "WARNING",
    }, svc_url))
    yield mock_url, svc_url
    for p in procs:
        p.terminate()
        p.wait(timeout=10)


async def test_ramp_with_20_percent_failures_loses_no_calls(stack):
    mock_url, svc_url = stack
    # 30 sim min in 3 real s; ~1,000 sim calls * 120/600 = ~200 real calls
    cfg = LoadConfig(target=svc_url, start="07:55", duration_min=30, compression=600, rate_scale=120, seed=1)

    report = await run_load(cfg, log=lambda *_: None)
    s = report["summary"]

    assert 0.7 * s["expected_calls"] <= s["calls_sent"] <= 1.3 * s["expected_calls"]
    assert s["lost_or_errored"] == 0
    assert s["by_status"].get("processed", 0) + s["by_status"].get("dead_lettered", 0) == s["calls_sent"]
    assert 0.12 <= s["initial_dependency_failure_rate"] <= 0.28
    assert s["final_call_failure_rate_before_replay"] < 0.05     # ~0.2^3 = 0.8% expected
    assert s["retries"] > 0
    assert s["dlq_depth_after_replay"] == 0
    # the generator followed the ramp: last window is clearly busier than the first
    assert report["timeline"][-1]["sent"] > report["timeline"][0]["sent"]


async def test_outage_opens_circuit_and_replay_drains_dlq(stack):
    mock_url, svc_url = stack
    cfg = LoadConfig(target=svc_url, mock_url=mock_url, start="08:30", duration_min=40, compression=600,
                     rate_scale=20, seed=2, outage_start="08:40", outage_min=10, replay_wait_s=1.5)

    report = await run_load(cfg, log=lambda *_: None)
    s = report["summary"]
    states = [t["to"] for t in s["circuit_transitions"]]

    assert s["lost_or_errored"] == 0
    assert "open" in states and states[-1] == "closed"       # opened during outage, recovered after
    assert s["circuit_rejections"] > 0                       # failed fast instead of hammering
    assert s["dlq_depth_after_run"] > 0
    assert s["replayed_ok"] == s["dlq_depth_after_run"]
    assert s["dlq_depth_after_replay"] == 0
    assert (httpx.get(f"{mock_url}/health").json()["failure_rate"]) == 0.2   # outage restored

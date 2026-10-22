"""Replays the Appendix C traffic profile against the inference service.

Time compression:
    --compression 60  ->  1 simulated hour = 1 real minute (the PDF's example).
    --rate-scale 1    ->  send the profile's calls/min as calls per *real* minute
                          (10/min baseline, 50/min peak).
    --rate-scale 60   ->  send the full simulated volume in compressed time
                          (600/min baseline, 3000/min peak). Use values in between
                          for a stress test that fits your laptop.

Arrivals are Poisson (random gaps) with the rate re-read from the schedule at
every arrival, so ramps are followed smoothly.

Example (3 real minutes covering baseline -> ramp -> hold -> back to baseline):
    python -m load_tests.traffic_generator --start 07:30 --duration-min 180 \
        --compression 60 --rate-scale 10 --mock-url http://localhost:8001 \
        --outage-start 09:00 --outage-min 10 --output results/demo.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx

from load_tests.traffic_profile import expected_calls, generate_event, parse_hhmm, rate_per_min

OK_STATUSES = {"processed", "duplicate", "dead_lettered"}
BUCKET_MIN = 15


@dataclass
class LoadConfig:
    target: str = "http://localhost:8000"
    mock_url: str | None = None
    start: str = "07:30"
    duration_min: float = 180
    compression: float = 60
    rate_scale: float = 1.0
    seed: int = 2026
    outage_start: str | None = None
    outage_min: float = 0
    replay: bool = True
    replay_rounds: int = 10
    replay_wait_s: float = 3.0
    max_outstanding: int = 2000
    request_timeout_s: float = 30.0


def _pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return round(s[min(len(s) - 1, int(p / 100 * len(s)))], 1)


def _fmt(minute: float) -> str:
    m = int(round(minute)) % 1440
    return f"{m // 60:02d}:{m % 60:02d}"


class _Outage:
    """Flips the mock's failure rate to 1.0 for a simulated time window."""

    def __init__(self, client: httpx.AsyncClient, cfg: LoadConfig, start_min: float):
        self.client = client
        self.mock_url = cfg.mock_url
        self.start = parse_hhmm(cfg.outage_start) if cfg.outage_start else None
        if self.start is not None and self.start < start_min:
            self.start += 1440
        self.end = self.start + cfg.outage_min if self.start is not None else None
        self.original: float | None = None
        self.active = False
        self.events: list[dict] = []

    async def tick(self, sim_min: float) -> None:
        if self.start is None or not self.mock_url:
            return
        if not self.active and self.original is None and sim_min >= self.start:
            self.original = (await self.client.get(f"{self.mock_url}/health")).json()["failure_rate"]
            await self._set(1.0, sim_min)
            self.active = True
        elif self.active and sim_min >= self.end:
            await self.restore(sim_min)

    async def restore(self, sim_min: float | None = None) -> None:
        if self.active:
            await self._set(self.original, sim_min)
            self.active = False

    async def _set(self, rate: float, sim_min: float | None) -> None:
        await self.client.put(f"{self.mock_url}/admin/failure-rate", json={"failure_rate": rate})
        self.events.append({"sim_time": _fmt(sim_min) if sim_min is not None else None,
                            "real_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                            "failure_rate": rate})


async def _send(client, sem, event, sim_min, results) -> None:
    async with sem:
        started = time.perf_counter()
        try:
            resp = await client.post("/calls", json=event)
            body = resp.json()
            status = body.get("status", f"http_{resp.status_code}")
            results.append({"sim_min": sim_min, "http": resp.status_code, "status": status,
                            "attempts": body.get("attempts"), "reason": body.get("failure_reason"),
                            "latency_ms": (time.perf_counter() - started) * 1000})
        except (httpx.HTTPError, ValueError) as exc:
            results.append({"sim_min": sim_min, "http": None, "status": f"client_error:{type(exc).__name__}",
                            "attempts": None, "reason": None,
                            "latency_ms": (time.perf_counter() - started) * 1000})


def _counter_delta(after: dict, before: dict) -> dict:
    return {k: v - (before.get(k) or 0) for k, v in after.items() if isinstance(v, (int, float))}


async def run_load(cfg: LoadConfig, log=print) -> dict:
    rng = random.Random(cfg.seed)
    start_min = parse_hhmm(cfg.start)
    end_min = start_min + cfg.duration_min
    results: list[dict] = []
    sem = asyncio.Semaphore(cfg.max_outstanding)
    limits = httpx.Limits(max_connections=cfg.max_outstanding, max_keepalive_connections=100)

    async with httpx.AsyncClient(base_url=cfg.target, timeout=cfg.request_timeout_s, limits=limits) as client, \
               httpx.AsyncClient(timeout=10) as admin:
        before = (await client.get("/metrics")).json()
        outage = _Outage(admin, cfg, start_min)
        tasks: list[asyncio.Task] = []
        log(f"replaying {_fmt(start_min)} -> {_fmt(end_min)} sim "
            f"({cfg.duration_min * 60 / cfg.compression:.0f}s real, compression {cfg.compression}x, "
            f"rate scale {cfg.rate_scale}x)")

        # Open-loop schedule: arrival times are fixed on an absolute timeline, so a
        # late wake-up (busy event loop) does not push back every later arrival.
        to_sim = lambda real_s: start_min + real_s * cfg.compression / 60  # noqa: E731
        end_real_s = cfg.duration_min * 60 / cfg.compression
        t0 = time.monotonic()
        next_real_s = 0.0
        last_log = -1
        try:
            while True:
                next_real_s += rng.expovariate(rate_per_min(to_sim(next_real_s)) * cfg.rate_scale / 60)
                if next_real_s >= end_real_s:
                    break
                delay = next_real_s - (time.monotonic() - t0)
                if delay > 0:
                    await asyncio.sleep(delay)
                sim = to_sim(next_real_s)
                await outage.tick(to_sim(time.monotonic() - t0))
                event = generate_event(rng, sim % 1440)
                tasks.append(asyncio.create_task(_send(client, sem, event, sim, results)))
                if int(sim) // BUCKET_MIN != last_log:
                    last_log = int(sim) // BUCKET_MIN
                    log(f"  sim {_fmt(sim)}  target {rate_per_min(sim):4.1f}/min  sent {len(tasks)}")
            remaining = end_real_s - (time.monotonic() - t0)
            if remaining > 0:
                await asyncio.sleep(remaining)
            await outage.tick(end_min)
            await asyncio.gather(*tasks)
        finally:
            await outage.restore()

        after_run = (await client.get("/metrics")).json()
        replay_rounds = []
        if cfg.replay:
            for _ in range(cfg.replay_rounds):
                depth = (await client.get("/metrics")).json()["dlq"]["depth"]
                if depth == 0:
                    break
                summary = (await client.post("/dlq/replay", json={"limit": 10_000, "concurrency": 8})).json()
                summary["dlq_depth_before"] = depth
                replay_rounds.append(summary)
                log(f"  replay: {summary['resolved']} resolved, {summary['failed_again']} failed again"
                    + (f", stopped ({summary['stopped_reason']})" if summary["stopped_reason"] else ""))
                if summary["stopped_reason"] or summary["failed_again"]:
                    await asyncio.sleep(cfg.replay_wait_s)
        final = (await client.get("/metrics")).json()

    return _report(cfg, results, before, after_run, final, replay_rounds, outage.events, start_min, end_min)


def _report(cfg, results, before, after_run, final, replay_rounds, outage_events, start_min, end_min) -> dict:
    statuses = Counter(r["status"] for r in results)
    lost = sum(n for s, n in statuses.items() if s not in OK_STATUSES)
    lat = [r["latency_ms"] for r in results if r["status"] == "processed"]

    buckets = defaultdict(list)
    for r in results:
        buckets[int((r["sim_min"] - start_min) // BUCKET_MIN)].append(r)
    timeline = []
    for i in range(int((end_min - start_min) // BUCKET_MIN) + 1):
        b0 = start_min + i * BUCKET_MIN
        b1 = min(b0 + BUCKET_MIN, end_min)
        if b1 <= b0:
            continue
        rows = buckets.get(i, [])
        c = Counter(r["status"] for r in rows)
        timeline.append({
            "sim_window": f"{_fmt(b0)}-{_fmt(b1)}",
            "target_per_sim_min": round(expected_calls(b0, b1) / (b1 - b0), 1),
            # calls per real minute = target * rate_scale, and a sim minute lasts 1/compression real minutes
            "achieved_per_sim_min": round(len(rows) * cfg.compression / ((b1 - b0) * cfg.rate_scale), 1),
            "sent": len(rows),
            "processed": c["processed"],
            "dead_lettered": c["dead_lettered"],
            "p50_ms": _pct([r["latency_ms"] for r in rows if r["status"] == "processed"], 50),
            "p99_ms": _pct([r["latency_ms"] for r in rows if r["status"] == "processed"], 99),
        })

    dep_delta = _counter_delta(after_run["dependency"], before["dependency"])
    attempts = dep_delta.get("attempts", 0)
    finished = statuses["processed"] + statuses["dead_lettered"]
    transitions = [t for t in final["circuit"]["transitions"] if t not in before["circuit"]["transitions"]]

    return {
        "config": asdict(cfg),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "summary": {
            "calls_sent": len(results),
            "expected_calls": round(expected_calls(start_min, end_min) * cfg.rate_scale / cfg.compression),
            "by_status": dict(statuses),
            "lost_or_errored": lost,
            "dependency_attempts": attempts,
            "dependency_failures": dep_delta.get("failures", 0),
            "initial_dependency_failure_rate": round(dep_delta.get("failures", 0) / attempts, 4) if attempts else None,
            "retries": dep_delta.get("retries", 0),
            "circuit_rejections": dep_delta.get("circuit_rejections", 0),
            "final_call_failure_rate_before_replay": round(statuses["dead_lettered"] / finished, 4) if finished else None,
            "client_latency_ms": {"p50": _pct(lat, 50), "p90": _pct(lat, 90), "p99": _pct(lat, 99)},
            "max_in_flight_seen": after_run["max_in_flight_seen"],
            "dlq_depth_after_run": after_run["dlq"]["depth"],
            "dlq_depth_after_replay": final["dlq"]["depth"],
            "replayed_ok": sum(r["resolved"] for r in replay_rounds),
            "circuit_transitions": transitions,
            "outage": outage_events,
        },
        "timeline": timeline,
        "replay_rounds": replay_rounds,
        "service_metrics_final": final,
    }


def print_report(report: dict) -> None:
    s = report["summary"]
    print("\n=== timeline (per 15 simulated minutes) ===")
    print(f"{'sim window':<13} {'target/min':>10} {'achieved/min':>12} {'sent':>6} {'ok':>6} {'DLQ':>5} {'p50 ms':>8} {'p99 ms':>8}")
    for t in report["timeline"]:
        print(f"{t['sim_window']:<13} {t['target_per_sim_min']:>10} {t['achieved_per_sim_min']:>12} {t['sent']:>6} "
              f"{t['processed']:>6} {t['dead_lettered']:>5} {t['p50_ms'] or '-':>8} {t['p99_ms'] or '-':>8}")
    print("\n=== summary ===")
    for k in ["calls_sent", "expected_calls", "by_status", "lost_or_errored", "dependency_attempts",
              "dependency_failures", "initial_dependency_failure_rate", "retries", "circuit_rejections",
              "final_call_failure_rate_before_replay", "client_latency_ms", "max_in_flight_seen",
              "dlq_depth_after_run", "replayed_ok", "dlq_depth_after_replay"]:
        print(f"{k:<40} {s[k]}")
    print(f"{'circuit_transitions':<40} {[(t['from'], t['to']) for t in s['circuit_transitions']]}")
    if s["outage"]:
        print(f"{'outage':<40} {s['outage']}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    d = LoadConfig()
    p.add_argument("--target", default=d.target)
    p.add_argument("--mock-url", default=d.mock_url, help="needed only for --outage-start")
    p.add_argument("--start", default=d.start, help="simulated start time HH:MM")
    p.add_argument("--duration-min", type=float, default=d.duration_min, help="simulated minutes to replay")
    p.add_argument("--compression", type=float, default=d.compression, help="simulated seconds per real second")
    p.add_argument("--rate-scale", type=float, default=d.rate_scale, help="multiplier on calls per real minute")
    p.add_argument("--seed", type=int, default=d.seed)
    p.add_argument("--outage-start", default=None, help="simulated HH:MM to force the mock to 100%% failures")
    p.add_argument("--outage-min", type=float, default=0, help="simulated minutes the outage lasts")
    p.add_argument("--no-replay", action="store_true", help="do not replay the DLQ at the end")
    p.add_argument("--output", default=None, help="write the full JSON report here")
    a = p.parse_args()

    cfg = LoadConfig(target=a.target, mock_url=a.mock_url, start=a.start, duration_min=a.duration_min,
                     compression=a.compression, rate_scale=a.rate_scale, seed=a.seed,
                     outage_start=a.outage_start, outage_min=a.outage_min, replay=not a.no_replay)
    report = asyncio.run(run_load(cfg))
    print_report(report)
    if a.output:
        Path(a.output).parent.mkdir(parents=True, exist_ok=True)
        Path(a.output).write_text(json.dumps(report, indent=2))
        print(f"\nfull report: {a.output}")


if __name__ == "__main__":
    main()

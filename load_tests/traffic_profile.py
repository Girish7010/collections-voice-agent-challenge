"""Appendix C traffic schedule and Appendix A synthetic call events.

All times are "simulated minutes since midnight" (0..1440, repeating daily).

    00:00-08:00  10 calls/min
    08:00-10:00  ramp 10 -> 50 over 15 min, then hold 50   (campaign A)
    10:00-14:00  10 calls/min
    14:00-16:00  ramp 10 -> 50 over 15 min, then hold 50   (campaign B)
    16:00-24:00  10 calls/min
"""

from __future__ import annotations

import random
import uuid
from datetime import datetime, timedelta, timezone

BASELINE = 10.0
PEAK = 50.0
RAMP_MIN = 15.0
CAMPAIGN_WINDOWS = {"A": (8 * 60, 10 * 60), "B": (14 * 60, 16 * 60)}
DEFAULT_DAY = datetime(2026, 10, 1, tzinfo=timezone.utc)

# Appendix A: "roughly a 60/20/15/5 split favoring connected / no_answer".
OUTCOMES = (["connected", "no_answer", "ptp_taken", "dispute"], [60, 20, 15, 5])
BUCKETS = (["0-30 DPD", "31-60 DPD", "61-90 DPD", "90+ DPD"], [60, 20, 15, 5])
# Appendix A gives no split for product_type; this one is our assumption.
PRODUCTS = (["credit_card", "bnpl", "personal_loan"], [40, 35, 25])
DATASET_FAILURE_SHARE = 0.20


def parse_hhmm(text: str) -> float:
    """'08:15' or '08:07:30' -> minutes since midnight."""
    parts = [int(p) for p in text.split(":")]
    if len(parts) not in (2, 3):
        raise ValueError(f"expected HH:MM[:SS], got {text!r}")
    h, m, s = (parts + [0])[:3]
    if not (0 <= h <= 23 and 0 <= m <= 59 and 0 <= s <= 59):
        raise ValueError(f"invalid time {text!r}")
    return h * 60 + m + s / 60


def campaign_window(sim_minute: float) -> str | None:
    m = sim_minute % 1440
    for name, (start, end) in CAMPAIGN_WINDOWS.items():
        if start <= m < end:
            return name
    return None


def rate_per_min(sim_minute: float) -> float:
    """Target calls per simulated minute at this simulated time."""
    m = sim_minute % 1440
    window = campaign_window(m)
    if window is None:
        return BASELINE
    into = m - CAMPAIGN_WINDOWS[window][0]
    if into < RAMP_MIN:
        return BASELINE + (PEAK - BASELINE) * into / RAMP_MIN
    return PEAK


def expected_calls(start_min: float, end_min: float, step: float = 1 / 60) -> float:
    """Integral of the rate between two simulated times (midpoint rule)."""
    total, t = 0.0, start_min
    while t < end_min:
        dt = min(step, end_min - t)
        total += rate_per_min(t + dt / 2) * dt
        t += dt
    return total


def generate_event(
    rng: random.Random,
    sim_minute: float,
    *,
    day: datetime = DEFAULT_DAY,
    include_failures: bool = False,
) -> dict:
    """One synthetic Appendix A call event. No real borrower data.

    Live traffic never carries outcome="failed_downstream": that outcome is what
    the service records when the pipeline fails. `include_failures=True` is for
    building a static sample dataset, where ~20% of rows are marked failed.
    """
    ts = day + timedelta(minutes=sim_minute)
    window = campaign_window(sim_minute)
    failed = include_failures and rng.random() < DATASET_FAILURE_SHARE
    outcome = "failed_downstream" if failed else rng.choices(*OUTCOMES)[0]
    talked = outcome in ("connected", "ptp_taken", "dispute")
    return {
        "event_id": str(uuid.UUID(int=rng.getrandbits(128), version=4)),
        "campaign_id": f"CAMP-{ts:%Y-%m-%d}-{window or 'BASE'}",
        "borrower_bucket": rng.choices(*BUCKETS)[0],
        "product_type": rng.choices(*PRODUCTS)[0],
        "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "call_duration_s": rng.randint(20, 180) if talked else 0,
        "outcome": outcome,
        "retry_count": rng.randint(1, 2) if failed else 0,
        "audio_ref": "synthetic",
    }

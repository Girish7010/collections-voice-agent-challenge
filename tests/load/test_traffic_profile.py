"""Appendix C traffic profile and Appendix A synthetic event generation."""

import random
from collections import Counter
from datetime import datetime, timezone

import pytest

from app.models import CallEvent
from load_tests.traffic_profile import expected_calls, generate_event, parse_hhmm, rate_per_min


@pytest.mark.parametrize("hhmm, rate", [
    ("00:00", 10), ("07:59", 10),
    ("08:00", 10), ("08:07:30", 30), ("08:15", 50), ("09:59", 50),
    ("10:00", 10), ("13:59", 10),
    ("14:00", 10), ("14:07:30", 30), ("14:15", 50), ("15:59", 50),
    ("16:00", 10), ("23:59", 10),
])
def test_rate_matches_appendix_c(hhmm, rate):
    assert rate_per_min(parse_hhmm(hhmm)) == pytest.approx(rate)


def test_profile_repeats_daily():
    assert rate_per_min(parse_hhmm("08:30") + 1440) == 50
    assert rate_per_min(parse_hhmm("12:00") + 2 * 1440) == 10


def test_expected_calls_for_a_full_day():
    # 20 baseline hours at 10/min = 12,000
    # each campaign window: 15-min ramp averaging 30/min (450) + 105 min at 50/min (5,250) = 5,700
    assert expected_calls(0, 1440) == pytest.approx(12_000 + 2 * 5_700, rel=1e-3)


def test_parse_hhmm():
    assert parse_hhmm("08:15") == 495
    assert parse_hhmm("08:07:30") == 487.5
    with pytest.raises(ValueError):
        parse_hhmm("25:00")


def test_generated_events_are_schema_valid_and_synthetic():
    rng = random.Random(1)
    sim_minute = parse_hhmm("08:30")
    ev = generate_event(rng, sim_minute, day=datetime(2026, 10, 1, tzinfo=timezone.utc))

    parsed = CallEvent.model_validate(ev)
    assert parsed.timestamp == datetime(2026, 10, 1, 8, 30, tzinfo=timezone.utc)
    assert parsed.audio_ref == "synthetic"
    assert parsed.campaign_id == "CAMP-2026-10-01-A"   # morning campaign window
    assert len(parsed.event_id) == 36                  # uuid4


def test_campaign_id_follows_the_window():
    rng = random.Random(1)
    day = datetime(2026, 10, 1, tzinfo=timezone.utc)
    assert generate_event(rng, parse_hhmm("14:30"), day=day)["campaign_id"] == "CAMP-2026-10-01-B"
    assert generate_event(rng, parse_hhmm("03:00"), day=day)["campaign_id"] == "CAMP-2026-10-01-BASE"


def test_event_ids_are_reproducible_with_a_seed():
    a = [generate_event(random.Random(5), 600)["event_id"] for _ in range(3)]
    b = [generate_event(random.Random(5), 600)["event_id"] for _ in range(3)]
    assert a == b


def test_distributions_roughly_follow_appendix_a():
    rng = random.Random(42)
    events = [generate_event(rng, 600) for _ in range(5000)]
    outcomes = Counter(e["outcome"] for e in events)
    buckets = Counter(e["borrower_bucket"] for e in events)

    assert "failed_downstream" not in outcomes     # assigned by the service, not the caller
    assert outcomes["connected"] / 5000 == pytest.approx(0.60, abs=0.03)
    assert outcomes["no_answer"] / 5000 == pytest.approx(0.20, abs=0.03)
    assert buckets["0-30 DPD"] / 5000 == pytest.approx(0.60, abs=0.03)
    assert all(e["call_duration_s"] == 0 for e in events if e["outcome"] == "no_answer")
    assert all(e["call_duration_s"] > 0 for e in events if e["outcome"] == "connected")


def test_dataset_mode_marks_about_20_percent_failed_downstream():
    rng = random.Random(7)
    events = [generate_event(rng, 600, include_failures=True) for _ in range(5000)]
    failed = [e for e in events if e["outcome"] == "failed_downstream"]

    assert len(failed) / 5000 == pytest.approx(0.20, abs=0.03)
    assert all(e["call_duration_s"] == 0 and e["retry_count"] >= 1 for e in failed)

"""Appendix A call-event schema."""

import pytest
from pydantic import ValidationError

from app.models import CallEvent

APPENDIX_A_SAMPLES = [
    {"event_id": "a1f2c3d4-0001", "campaign_id": "CAMP-2026-09-A", "borrower_bucket": "0-30 DPD", "product_type": "bnpl", "timestamp": "2026-09-01T09:03:12Z", "call_duration_s": 47, "outcome": "connected", "retry_count": 0},
    {"event_id": "a1f2c3d4-0002", "campaign_id": "CAMP-2026-09-A", "borrower_bucket": "31-60 DPD", "product_type": "credit_card", "timestamp": "2026-09-01T09:04:01Z", "call_duration_s": 0, "outcome": "failed_downstream", "retry_count": 2},
    {"event_id": "a1f2c3d4-0003", "campaign_id": "CAMP-2026-09-A", "borrower_bucket": "61-90 DPD", "product_type": "personal_loan", "timestamp": "2026-09-01T09:04:45Z", "call_duration_s": 112, "outcome": "ptp_taken", "retry_count": 0},
    {"event_id": "a1f2c3d4-0004", "campaign_id": "CAMP-2026-09-A", "borrower_bucket": "0-30 DPD", "product_type": "bnpl", "timestamp": "2026-09-01T09:05:30Z", "call_duration_s": 0, "outcome": "no_answer", "retry_count": 1},
    {"event_id": "a1f2c3d4-0005", "campaign_id": "CAMP-2026-09-A", "borrower_bucket": "90+ DPD", "product_type": "credit_card", "timestamp": "2026-09-01T09:06:02Z", "call_duration_s": 89, "outcome": "dispute", "retry_count": 0},
]

REQUIRED_ONLY = {"event_id": "e", "campaign_id": "c", "borrower_bucket": "0-30 DPD",
                 "timestamp": "2026-09-01T09:03:12Z", "outcome": "connected"}


@pytest.mark.parametrize("sample", APPENDIX_A_SAMPLES)
def test_appendix_a_samples_are_valid(sample):
    assert CallEvent.model_validate(sample).event_id == sample["event_id"]


def test_only_required_fields_needed():
    ev = CallEvent.model_validate(REQUIRED_ONLY)
    assert ev.audio_ref == "synthetic"
    assert ev.product_type is None


@pytest.mark.parametrize("field", ["event_id", "campaign_id", "borrower_bucket", "timestamp", "outcome"])
def test_required_fields_are_enforced(field):
    body = {k: v for k, v in REQUIRED_ONLY.items() if k != field}
    with pytest.raises(ValidationError):
        CallEvent.model_validate(body)


@pytest.mark.parametrize("override", [
    {"borrower_bucket": "120 DPD"}, {"product_type": "mortgage"}, {"outcome": "hung_up"},
    {"call_duration_s": -1}, {"retry_count": -1}, {"timestamp": "yesterday"}, {"event_id": ""},
])
def test_invalid_values_are_rejected(override):
    with pytest.raises(ValidationError):
        CallEvent.model_validate({**REQUIRED_ONLY, **override})

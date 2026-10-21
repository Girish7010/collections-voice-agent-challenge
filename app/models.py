"""Call-event schema (challenge Appendix A).

Note: the JSON schema says event_id has `"format": "uuid"`, but the Appendix A
sample records use ids like "a1f2c3d4-0001". To accept the official samples we
require a non-empty string; the traffic generator emits real UUIDs.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

BorrowerBucket = Literal["0-30 DPD", "31-60 DPD", "61-90 DPD", "90+ DPD"]
ProductType = Literal["credit_card", "bnpl", "personal_loan"]
Outcome = Literal["connected", "no_answer", "ptp_taken", "dispute", "failed_downstream"]


class CallEvent(BaseModel):
    event_id: str = Field(min_length=1, max_length=128)
    campaign_id: str = Field(min_length=1, max_length=128)
    borrower_bucket: BorrowerBucket
    product_type: ProductType | None = None
    timestamp: datetime
    call_duration_s: int | None = Field(default=None, ge=0)
    outcome: Outcome
    retry_count: int | None = Field(default=None, ge=0)
    # Not in Appendix A: reference to the call audio. Always "synthetic" here.
    audio_ref: str = "synthetic"

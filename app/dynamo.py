"""Shared DynamoDB plumbing for the dead-letter and ledger stores.

Why DynamoDB for both, when docs/architecture.md said SQS for the dead-letter queue:
SQS is a message pipe, not a queryable store. It has no lookup by key, no filter by
status, no count by status and no in-place update, so it cannot serve GET /dlq,
GET /dlq/{event_id} or the claim/resolve lifecycle in app/dlq.py. SQS still has a
place as a durable replay trigger, but the record of a dead letter has to live
somewhere queryable. See docs/architecture.md for the corrected plan.

Numbers: boto3's resource layer refuses float and insists on Decimal, so values
cross the boundary through _num() on the way in and _plain() on the way out.
"""

from __future__ import annotations

import os
from decimal import Decimal
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

# Raised by a failed ConditionExpression. Both stores treat this as "someone else
# won the race", never as an error.
CONDITION_FAILED = "ConditionalCheckFailedException"


def is_condition_failure(exc: ClientError) -> bool:
    return exc.response.get("Error", {}).get("Code") == CONDITION_FAILED


def table(name: str, *, region: str | None = None, endpoint_url: str | None = None):
    """A DynamoDB Table resource.

    Credentials come from the standard boto3 chain. endpoint_url exists for
    DynamoDB Local; tests use moto, which patches botocore instead.
    """
    session = boto3.session.Session(region_name=region or os.getenv("AWS_REGION") or None)
    # Retries: adaptive mode backs off on throttling. The caller already sits behind
    # the service's own retry policy, so keep the attempt count low.
    cfg = Config(retries={"max_attempts": 3, "mode": "adaptive"})
    return session.resource("dynamodb", endpoint_url=endpoint_url, config=cfg).Table(name)


def to_decimal(value: float | int) -> Decimal:
    """float -> Decimal without binary float artefacts (Decimal(0.1) is not 0.1).

    DynamoDB has one numeric type and boto3's resource layer rejects float, so every
    number this package writes goes through here.
    """
    return Decimal(str(value))


def _plain(value: Any) -> Any:
    """Decimal -> int when integral, else float. Everything else passes through."""
    if isinstance(value, Decimal):
        as_int = int(value)
        return as_int if value == as_int else float(value)
    return value


def plain_item(item: dict | None) -> dict | None:
    """Convert a whole item's Decimals back to int/float."""
    if item is None:
        return None
    return {k: _plain(v) for k, v in item.items()}

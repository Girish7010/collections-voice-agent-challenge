"""The whole service over HTTP, backed by DynamoDB instead of SQLite.

test_store_parity.py proves the two backends behave alike in isolation. This exercises
the real API surface on the DynamoDB backend with a widened store thread pool, which is
the configuration the Terraform stack deploys - so the pieces are checked together:
create_app -> wiring.build_stores -> DynamoDB, across worker threads.

The dependency is a fake adapter, so nothing here touches the network or a real account.
"""

from __future__ import annotations

import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

from app.api import create_app
from app.config import Settings
from app.dependency_adapter import DependencyError, PipelineResult

REGION = "ap-south-1"
DLQ_TABLE = "itest-dead-letters"
LEDGER_TABLE = "itest-event-ledger"

OK = PipelineResult(transcript="synthetic transcript", response_audio_ref="synthetic", latency_ms=42)


class SwitchableAdapter:
    def __init__(self):
        self.failing = False

    async def process(self, request):
        if self.failing:
            raise DependencyError("downstream_timeout", retryable=True, status_code=503)
        return OK


def event(i=1):
    return {"event_id": f"evt-{i}", "campaign_id": "CAMP-2026-09-A", "borrower_bucket": "0-30 DPD",
            "product_type": "bnpl", "timestamp": "2026-09-01T09:03:12Z", "outcome": "connected"}


def _create_tables():
    ddb = boto3.client("dynamodb", region_name=REGION)
    ddb.create_table(
        TableName=DLQ_TABLE,
        BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": "event_id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "event_id", "AttributeType": "S"},
            {"AttributeName": "status", "AttributeType": "S"},
            {"AttributeName": "seq", "AttributeType": "N"},
        ],
        GlobalSecondaryIndexes=[{
            "IndexName": "status-seq-index",
            "KeySchema": [{"AttributeName": "status", "KeyType": "HASH"},
                          {"AttributeName": "seq", "KeyType": "RANGE"}],
            "Projection": {"ProjectionType": "ALL"},
        }],
    )
    ddb.create_table(
        TableName=LEDGER_TABLE,
        BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": "event_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "event_id", "AttributeType": "S"}],
    )


@pytest.fixture
def stack(monkeypatch):
    for k, v in {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
                 "AWS_SESSION_TOKEN": "testing", "AWS_DEFAULT_REGION": REGION}.items():
        monkeypatch.setenv(k, v)

    with mock_aws():
        _create_tables()
        settings = Settings(
            store_backend="dynamodb", dlq_table=DLQ_TABLE, ledger_table=LEDGER_TABLE,
            aws_region=REGION,
            # Wider than 1: proves store calls work off the main thread, which is the
            # whole point of making the pool configurable for DynamoDB.
            db_threads=4,
            base_delay_s=0, max_delay_s=0, max_attempts=2,
            cb_window=100, cb_min_calls=100,  # keep the breaker out of the way
        )
        adapter = SwitchableAdapter()
        with TestClient(create_app(settings, adapter=adapter)) as client:
            yield client, adapter


def test_health_reports_ok(stack):
    client, _ = stack

    assert client.get("/health").json()["status"] == "ok"


def test_call_is_processed_and_a_resubmission_is_a_duplicate(stack):
    client, _ = stack

    first = client.post("/calls", json=event())
    assert first.status_code == 200
    assert first.json()["status"] == "processed"

    again = client.post("/calls", json=event())
    assert again.status_code == 200
    assert again.json()["status"] == "duplicate"
    # The stored result comes back from the ledger, not from a second pipeline run.
    assert again.json()["result"]["transcript"] == "synthetic transcript"


def test_failures_are_dead_lettered_then_replayed_to_empty(stack):
    client, adapter = stack

    adapter.failing = True
    resp = client.post("/calls", json=event(7))
    assert resp.status_code == 202
    assert resp.json()["status"] == "dead_lettered"

    listed = client.get("/dlq").json()
    assert [i["event_id"] for i in listed["items"]] == ["evt-7"]
    assert listed["counts"] == {"pending": 1}
    assert client.get("/metrics").json()["dlq"]["depth"] == 1

    one = client.get("/dlq/evt-7").json()
    assert one["failure_reason"] == "downstream_timeout"
    assert one["status"] == "pending"

    # Dependency recovers, replay drains the queue.
    adapter.failing = False
    replay = client.post("/dlq/replay").json()
    assert (replay["attempted"], replay["resolved"], replay["failed_again"]) == (1, 1, 0)

    assert client.get("/metrics").json()["dlq"]["depth"] == 0
    assert client.get("/dlq/evt-7").json()["status"] == "resolved"


def test_unknown_dead_letter_is_404(stack):
    client, _ = stack

    assert client.get("/dlq/nope").status_code == 404


def test_dlq_listing_filters_and_pages(stack):
    client, adapter = stack
    adapter.failing = True
    for i in range(4):
        client.post("/calls", json=event(i))

    pending = client.get("/dlq", params={"status": "pending", "limit": 2}).json()

    assert [i["event_id"] for i in pending["items"]] == ["evt-0", "evt-1"]
    assert client.get("/metrics").json()["dlq"]["depth"] == 4

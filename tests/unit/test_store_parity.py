"""One contract, two backends.

Every test here runs twice: once against SQLite and once against DynamoDB (moto).
Anything that passes for one backend and fails for the other is a parity bug, which
matters because the processor is written against the protocols in app/stores.py and
cannot tell the two apart.

tests/unit/test_dlq.py keeps the SQLite-only cases (such as inspecting rows with a
plain sqlite3 connection); this file covers the shared contract.
"""

from __future__ import annotations

import threading
import time

import pytest
from moto import mock_aws

from app.dlq import DeadLetterQueue
from app.dynamo_dlq import DynamoDeadLetterQueue
from app.dynamo_idempotency import DynamoEventLedger
from app.idempotency import ClaimResult, EventLedger
from app.stores import DeadLetterStore, LedgerStore

EVENT = {"event_id": "evt-1", "campaign_id": "CAMP-2026-09-A", "borrower_bucket": "0-30 DPD",
         "timestamp": "2026-09-01T09:03:12Z", "outcome": "connected"}

DLQ_TABLE = "test-dead-letters"
LEDGER_TABLE = "test-event-ledger"
REGION = "ap-south-1"


def _create_tables():
    import boto3

    ddb = boto3.client("dynamodb", region_name=REGION)
    # Mirrors infra/terraform/dynamodb.tf. Kept in step by test_terraform_schema below.
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
            "KeySchema": [
                {"AttributeName": "status", "KeyType": "HASH"},
                {"AttributeName": "seq", "KeyType": "RANGE"},
            ],
            "Projection": {"ProjectionType": "ALL"},
        }],
    )
    ddb.create_table(
        TableName=LEDGER_TABLE,
        BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": "event_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "event_id", "AttributeType": "S"}],
    )


class _Backend:
    """Builds fresh store instances, the way a new process or task would."""

    def __init__(self, kind: str, tmp_path):
        self.kind = kind
        self._path = str(tmp_path / "parity.db")

    def dlq(self, clock=time.time):
        if self.kind == "sqlite":
            return DeadLetterQueue(self._path, clock=clock)
        return DynamoDeadLetterQueue(DLQ_TABLE, region=REGION, clock=clock)

    def ledger(self, clock=time.time, ttl_days: int = 30):
        if self.kind == "sqlite":
            return EventLedger(self._path, clock=clock)
        return DynamoEventLedger(LEDGER_TABLE, region=REGION, clock=clock, ttl_days=ttl_days)


@pytest.fixture(params=["sqlite", "dynamodb"])
def backend(request, tmp_path, monkeypatch):
    if request.param == "sqlite":
        yield _Backend("sqlite", tmp_path)
        return

    # moto refuses to start without credentials in the environment, and must never
    # pick up real ones.
    for k, v in {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
                 "AWS_SECURITY_TOKEN": "testing", "AWS_SESSION_TOKEN": "testing",
                 "AWS_DEFAULT_REGION": REGION}.items():
        monkeypatch.setenv(k, v)

    with mock_aws():
        _create_tables()
        yield _Backend("dynamodb", tmp_path)


# ---------------- the protocols are actually satisfied ----------------

def test_both_backends_satisfy_the_protocols(backend):
    assert isinstance(backend.dlq(), DeadLetterStore)
    assert isinstance(backend.ledger(), LedgerStore)


# ---------------- dead-letter queue ----------------

def test_add_persists_all_required_fields(backend):
    backend.dlq().add(EVENT, reason="downstream_timeout", attempts=3)

    row = backend.dlq().get("evt-1")  # fresh instance: really persisted

    assert row.event_id == "evt-1"
    assert row.campaign_id == "CAMP-2026-09-A"
    assert row.failure_reason == "downstream_timeout"
    assert row.attempts == 3
    assert row.retry_count == 2
    assert row.status == "pending"
    assert row.failure_count == 1
    assert row.replay_attempts == 0
    assert row.first_failed_at and row.last_failed_at
    assert row.payload == EVENT


def test_get_unknown_event_is_none(backend):
    assert backend.dlq().get("nope") is None


def test_event_without_campaign_id_is_allowed(backend):
    backend.dlq().add({"event_id": "evt-nc"}, reason="circuit_open", attempts=0)

    assert backend.dlq().get("evt-nc").campaign_id is None


def test_adding_same_event_again_updates_instead_of_duplicating(backend):
    dlq = backend.dlq()
    dlq.add(EVENT, reason="downstream_timeout", attempts=3)
    dlq.add(EVENT, reason="circuit_open", attempts=0)

    rows = dlq.list()
    assert len(rows) == 1
    assert rows[0].failure_reason == "circuit_open"
    assert rows[0].failure_count == 2
    assert rows[0].retry_count == 0


def test_list_filters_by_status_and_depth_counts_pending(backend):
    dlq = backend.dlq()
    for i in range(3):
        dlq.add({**EVENT, "event_id": f"evt-{i}"}, reason="downstream_timeout", attempts=3)
    dlq.resolve("evt-0", resolved_by="replay")

    assert [r.event_id for r in dlq.list(status="pending")] == ["evt-1", "evt-2"]
    assert dlq.depth() == 2
    assert dlq.counts() == {"pending": 2, "resolved": 1}


def test_list_is_in_insertion_order_and_pages(backend):
    dlq = backend.dlq()
    for i in range(5):
        dlq.add({**EVENT, "event_id": f"evt-{i}"}, reason="downstream_timeout", attempts=3)

    assert [r.event_id for r in dlq.list()] == [f"evt-{i}" for i in range(5)]
    assert [r.event_id for r in dlq.list(limit=2)] == ["evt-0", "evt-1"]
    assert [r.event_id for r in dlq.list(limit=2, offset=2)] == ["evt-2", "evt-3"]
    assert [r.event_id for r in dlq.list(limit=2, offset=4)] == ["evt-4"]


def test_counts_omits_empty_statuses(backend):
    dlq = backend.dlq()
    dlq.add(EVENT, reason="downstream_timeout", attempts=3)

    assert dlq.counts() == {"pending": 1}


def test_claim_is_exclusive_and_counts_replay_attempts(backend):
    dlq = backend.dlq()
    dlq.add(EVENT, reason="downstream_timeout", attempts=3)

    assert dlq.claim("evt-1") is True
    assert dlq.claim("evt-1") is False  # second replayer loses
    row = dlq.get("evt-1")
    assert row.status == "replaying"
    assert row.replay_attempts == 1


def test_claim_of_unknown_event_is_false(backend):
    assert backend.dlq().claim("nope") is False


def test_resolved_record_cannot_be_claimed(backend):
    dlq = backend.dlq()
    dlq.add(EVENT, reason="downstream_timeout", attempts=3)
    dlq.resolve("evt-1", resolved_by="replay")

    assert dlq.claim("evt-1") is False


def test_claim_is_exclusive_across_threads(backend):
    backend.dlq().add(EVENT, reason="downstream_timeout", attempts=3)
    wins = []
    lock = threading.Lock()

    def worker():
        won = backend.dlq().claim("evt-1")
        with lock:
            wins.append(won)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert wins.count(True) == 1


def test_stale_claim_can_be_taken_over(backend):
    now = [1000.0]
    dlq = backend.dlq(clock=lambda: now[0])
    dlq.add(EVENT, reason="downstream_timeout", attempts=3)
    assert dlq.claim("evt-1", lease_s=60)

    now[0] += 30
    assert dlq.claim("evt-1", lease_s=60) is False
    now[0] += 31
    assert dlq.claim("evt-1", lease_s=60) is True  # replayer crashed; lease expired


def test_failed_replay_goes_back_to_pending(backend):
    dlq = backend.dlq()
    dlq.add(EVENT, reason="downstream_timeout", attempts=3)
    dlq.claim("evt-1")

    dlq.add(EVENT, reason="downstream_timeout", attempts=3)  # replay failed again

    row = dlq.get("evt-1")
    assert row.status == "pending"
    assert row.failure_count == 2
    assert row.replay_attempts == 1  # replay history survives the re-add


def test_resolve_records_who_resolved_it(backend):
    dlq = backend.dlq()
    dlq.add(EVENT, reason="downstream_timeout", attempts=3)

    assert dlq.resolve("evt-1", resolved_by="replay") is True
    assert dlq.resolve("unknown", resolved_by="replay") is False

    row = dlq.get("evt-1")
    assert row.status == "resolved"
    assert row.resolved_by == "replay"
    assert row.resolved_at


def test_resolving_twice_is_false_the_second_time(backend):
    dlq = backend.dlq()
    dlq.add(EVENT, reason="downstream_timeout", attempts=3)

    assert dlq.resolve("evt-1", resolved_by="replay") is True
    assert dlq.resolve("evt-1", resolved_by="replay") is False


def test_unclaim_returns_row_to_pending(backend):
    dlq = backend.dlq()
    dlq.add(EVENT, reason="downstream_timeout", attempts=3)
    dlq.claim("evt-1")

    dlq.unclaim("evt-1")

    assert dlq.get("evt-1").status == "pending"
    assert dlq.claim("evt-1") is True  # claimable again


def test_unclaim_on_a_pending_record_is_a_noop(backend):
    dlq = backend.dlq()
    dlq.add(EVENT, reason="downstream_timeout", attempts=3)

    dlq.unclaim("evt-1")  # must not raise
    dlq.unclaim("unknown")

    assert dlq.get("evt-1").status == "pending"


# ---------------- idempotency ledger ----------------

def test_first_claim_wins_then_in_progress(backend):
    ledger = backend.ledger()

    assert ledger.try_claim("evt-1").result is ClaimResult.CLAIMED
    assert ledger.try_claim("evt-1").result is ClaimResult.IN_PROGRESS


def test_completed_event_is_reported_as_duplicate_with_stored_result(backend):
    ledger = backend.ledger()
    ledger.try_claim("evt-1")
    ledger.complete("evt-1", {"transcript": "synthetic transcript"})

    claim = backend.ledger().try_claim("evt-1")

    assert claim.result is ClaimResult.DUPLICATE
    assert claim.stored_result == {"transcript": "synthetic transcript"}


def test_released_claim_can_be_claimed_again(backend):
    ledger = backend.ledger()
    ledger.try_claim("evt-1")
    ledger.release("evt-1")

    assert ledger.try_claim("evt-1").result is ClaimResult.CLAIMED


def test_release_never_deletes_a_completed_event(backend):
    ledger = backend.ledger()
    ledger.try_claim("evt-1")
    ledger.complete("evt-1", {})
    ledger.release("evt-1")

    assert ledger.try_claim("evt-1").result is ClaimResult.DUPLICATE


def test_complete_does_not_resurrect_a_released_event(backend):
    """A late complete() for an event that was already released must not create a
    'succeeded' record out of nothing - that would mask a genuine retry."""
    ledger = backend.ledger()
    ledger.try_claim("evt-1")
    ledger.release("evt-1")

    ledger.complete("evt-1", {"transcript": "late"})

    assert ledger.try_claim("evt-1").result is ClaimResult.CLAIMED


def test_stale_in_progress_claim_expires(backend):
    now = [0.0]
    ledger = backend.ledger(clock=lambda: now[0])
    ledger.try_claim("evt-1", lease_s=30)

    now[0] = 31
    assert ledger.try_claim("evt-1", lease_s=30).result is ClaimResult.CLAIMED


def test_fresh_in_progress_claim_is_not_stolen(backend):
    now = [0.0]
    ledger = backend.ledger(clock=lambda: now[0])
    ledger.try_claim("evt-1", lease_s=30)

    now[0] = 29
    assert ledger.try_claim("evt-1", lease_s=30).result is ClaimResult.IN_PROGRESS


def test_ledger_claim_is_exclusive_across_threads(backend):
    backend.ledger()  # create the schema up front; 8 threads racing to do it deadlocks SQLite
    results = []
    lock = threading.Lock()

    def worker():
        r = backend.ledger().try_claim("evt-1").result
        with lock:
            results.append(r)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(ClaimResult.CLAIMED) == 1


# ---------------- backend selection ----------------

def test_wiring_defaults_to_sqlite():
    from app.config import Settings
    from app.wiring import build_stores

    dlq, ledger = build_stores(Settings(db_path="/tmp/parity-default.db"))

    assert type(dlq) is DeadLetterQueue
    assert type(ledger) is EventLedger


def test_wiring_selects_dynamodb_and_passes_settings_through(monkeypatch):
    from app.config import Settings
    from app.wiring import build_stores

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")

    with mock_aws():
        _create_tables()
        dlq, ledger = build_stores(Settings(
            store_backend="dynamodb", dlq_table=DLQ_TABLE, ledger_table=LEDGER_TABLE,
            aws_region=REGION, ledger_ttl_days=7, db_threads=8,
        ))

        assert type(dlq) is DynamoDeadLetterQueue
        assert type(ledger) is DynamoEventLedger
        assert dlq.table_name == DLQ_TABLE
        assert ledger.table_name == LEDGER_TABLE
        assert ledger.ttl_days == 7


def test_completed_record_gets_a_ttl_stamp(monkeypatch):
    """Succeeded ledger records must carry expires_at, or the table grows forever."""
    import boto3

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")

    with mock_aws():
        _create_tables()
        ledger = DynamoEventLedger(LEDGER_TABLE, region=REGION, clock=lambda: 1_000_000.0, ttl_days=7)
        ledger.try_claim("evt-ttl")
        ledger.complete("evt-ttl", {"ok": True})

        item = boto3.resource("dynamodb", region_name=REGION).Table(LEDGER_TABLE) \
            .get_item(Key={"event_id": "evt-ttl"})["Item"]

        assert int(item["expires_at"]) == 1_000_000 + 7 * 86400


def test_ttl_days_zero_keeps_records_forever(monkeypatch):
    import boto3

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")

    with mock_aws():
        _create_tables()
        ledger = DynamoEventLedger(LEDGER_TABLE, region=REGION, ttl_days=0)
        ledger.try_claim("evt-keep")
        ledger.complete("evt-keep", {})

        item = boto3.resource("dynamodb", region_name=REGION).Table(LEDGER_TABLE) \
            .get_item(Key={"event_id": "evt-keep"})["Item"]

        assert "expires_at" not in item

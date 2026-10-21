"""SQLite dead-letter queue and idempotency ledger."""

import sqlite3
import threading

import pytest

from app.dlq import DeadLetterQueue
from app.idempotency import ClaimResult, EventLedger

EVENT = {"event_id": "evt-1", "campaign_id": "CAMP-2026-09-A", "borrower_bucket": "0-30 DPD",
         "timestamp": "2026-09-01T09:03:12Z", "outcome": "connected"}


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test.db")


# ---------------- dead-letter queue ----------------

def test_add_persists_all_required_fields(db_path):
    dlq = DeadLetterQueue(db_path)
    dlq.add(EVENT, reason="downstream_timeout", attempts=3)

    row = DeadLetterQueue(db_path).get("evt-1")   # fresh instance: really on disk

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


def test_adding_same_event_again_updates_instead_of_duplicating(db_path):
    dlq = DeadLetterQueue(db_path)
    dlq.add(EVENT, reason="downstream_timeout", attempts=3)
    dlq.add(EVENT, reason="circuit_open", attempts=0)

    rows = dlq.list()
    assert len(rows) == 1
    assert rows[0].failure_reason == "circuit_open"
    assert rows[0].failure_count == 2
    assert rows[0].retry_count == 0


def test_list_filters_by_status_and_depth_counts_pending(db_path):
    dlq = DeadLetterQueue(db_path)
    for i in range(3):
        dlq.add({**EVENT, "event_id": f"evt-{i}"}, reason="downstream_timeout", attempts=3)
    dlq.resolve("evt-0", resolved_by="replay")

    assert [r.event_id for r in dlq.list(status="pending")] == ["evt-1", "evt-2"]
    assert dlq.depth() == 2
    assert dlq.counts() == {"pending": 2, "resolved": 1}


def test_claim_is_exclusive_and_counts_replay_attempts(db_path):
    dlq = DeadLetterQueue(db_path)
    dlq.add(EVENT, reason="downstream_timeout", attempts=3)

    assert dlq.claim("evt-1") is True
    assert dlq.claim("evt-1") is False            # second replayer loses
    row = dlq.get("evt-1")
    assert row.status == "replaying"
    assert row.replay_attempts == 1


def test_claim_is_exclusive_across_threads(db_path):
    dlq = DeadLetterQueue(db_path)
    dlq.add(EVENT, reason="downstream_timeout", attempts=3)
    wins = []

    def worker():
        wins.append(DeadLetterQueue(db_path).claim("evt-1"))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert wins.count(True) == 1


def test_stale_claim_can_be_taken_over(db_path):
    now = [1000.0]
    dlq = DeadLetterQueue(db_path, clock=lambda: now[0])
    dlq.add(EVENT, reason="downstream_timeout", attempts=3)
    assert dlq.claim("evt-1", lease_s=60)

    now[0] += 30
    assert dlq.claim("evt-1", lease_s=60) is False
    now[0] += 31
    assert dlq.claim("evt-1", lease_s=60) is True   # replayer crashed; lease expired


def test_failed_replay_goes_back_to_pending(db_path):
    dlq = DeadLetterQueue(db_path)
    dlq.add(EVENT, reason="downstream_timeout", attempts=3)
    dlq.claim("evt-1")

    dlq.add(EVENT, reason="downstream_timeout", attempts=3)   # replay failed again

    row = dlq.get("evt-1")
    assert row.status == "pending"
    assert row.failure_count == 2
    assert row.replay_attempts == 1


def test_resolve_records_who_resolved_it(db_path):
    dlq = DeadLetterQueue(db_path)
    dlq.add(EVENT, reason="downstream_timeout", attempts=3)

    assert dlq.resolve("evt-1", resolved_by="replay") is True
    assert dlq.resolve("unknown", resolved_by="replay") is False

    row = dlq.get("evt-1")
    assert row.status == "resolved"
    assert row.resolved_by == "replay"
    assert row.resolved_at


def test_unclaim_returns_row_to_pending(db_path):
    dlq = DeadLetterQueue(db_path)
    dlq.add(EVENT, reason="downstream_timeout", attempts=3)
    dlq.claim("evt-1")

    dlq.unclaim("evt-1")

    assert dlq.get("evt-1").status == "pending"


def test_rows_are_inspectable_with_plain_sqlite(db_path):
    DeadLetterQueue(db_path).add(EVENT, reason="downstream_timeout", attempts=3)

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT event_id, failure_reason, status FROM dead_letters").fetchall()

    assert rows == [("evt-1", "downstream_timeout", "pending")]


# ---------------- idempotency ledger ----------------

def test_first_claim_wins_then_in_progress(db_path):
    ledger = EventLedger(db_path)

    assert ledger.try_claim("evt-1").result is ClaimResult.CLAIMED
    assert ledger.try_claim("evt-1").result is ClaimResult.IN_PROGRESS


def test_completed_event_is_reported_as_duplicate_with_stored_result(db_path):
    ledger = EventLedger(db_path)
    ledger.try_claim("evt-1")
    ledger.complete("evt-1", {"transcript": "synthetic transcript"})

    claim = EventLedger(db_path).try_claim("evt-1")

    assert claim.result is ClaimResult.DUPLICATE
    assert claim.stored_result == {"transcript": "synthetic transcript"}


def test_released_claim_can_be_claimed_again(db_path):
    ledger = EventLedger(db_path)
    ledger.try_claim("evt-1")
    ledger.release("evt-1")

    assert ledger.try_claim("evt-1").result is ClaimResult.CLAIMED


def test_release_never_deletes_a_completed_event(db_path):
    ledger = EventLedger(db_path)
    ledger.try_claim("evt-1")
    ledger.complete("evt-1", {})
    ledger.release("evt-1")

    assert ledger.try_claim("evt-1").result is ClaimResult.DUPLICATE


def test_stale_in_progress_claim_expires(db_path):
    now = [0.0]
    ledger = EventLedger(db_path, clock=lambda: now[0])
    ledger.try_claim("evt-1", lease_s=30)

    now[0] = 31
    assert ledger.try_claim("evt-1", lease_s=30).result is ClaimResult.CLAIMED


def test_ledger_claim_is_exclusive_across_threads(db_path):
    EventLedger(db_path)  # create schema first
    results = []

    def worker():
        results.append(EventLedger(db_path).try_claim("evt-1").result)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(ClaimResult.CLAIMED) == 1

"""Idempotency ledger: guarantees each event_id is sent through the pipeline
successfully at most once, whether it arrives live, is resubmitted by a client,
or is replayed from the DLQ.

    try_claim(id)  INSERT OR IGNORE an 'in_progress' row. Only one caller wins.
                   A 'succeeded' row means DUPLICATE (stored result returned).
                   An 'in_progress' row older than the lease is taken over
                   (the previous worker crashed).
    complete(id)   mark 'succeeded' and store the result.
    release(id)    delete an 'in_progress' row after a failure, so the event
                   can be retried or replayed later. Never touches 'succeeded'.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from app.db import connect, init_db, iso


class ClaimResult(str, Enum):
    CLAIMED = "claimed"
    DUPLICATE = "duplicate"
    IN_PROGRESS = "in_progress"


@dataclass(frozen=True)
class Claim:
    result: ClaimResult
    stored_result: dict | None = None


class EventLedger:
    def __init__(self, path: str, clock: Callable[[], float] = time.time):
        self.path = path
        self._clock = clock
        init_db(path)

    def try_claim(self, event_id: str, lease_s: float = 30.0) -> Claim:
        now = self._clock()
        with connect(self.path) as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO event_ledger (event_id, status, claimed_at) VALUES (?, 'in_progress', ?)",
                (event_id, now),
            )
            if cur.rowcount == 1:
                return Claim(ClaimResult.CLAIMED)

            row = conn.execute("SELECT status, claimed_at, result FROM event_ledger WHERE event_id = ?",
                               (event_id,)).fetchone()
            if row is None:  # released between our INSERT and SELECT; let the caller retry later
                return Claim(ClaimResult.IN_PROGRESS)
            if row["status"] == "succeeded":
                return Claim(ClaimResult.DUPLICATE, json.loads(row["result"]) if row["result"] else None)
            if row["claimed_at"] < now - lease_s:
                cur = conn.execute(
                    "UPDATE event_ledger SET claimed_at = ? WHERE event_id = ? AND status = 'in_progress' AND claimed_at = ?",
                    (now, event_id, row["claimed_at"]),
                )
                if cur.rowcount == 1:
                    return Claim(ClaimResult.CLAIMED)
            return Claim(ClaimResult.IN_PROGRESS)

    def complete(self, event_id: str, result: dict) -> None:
        with connect(self.path) as conn:
            conn.execute(
                "UPDATE event_ledger SET status = 'succeeded', completed_at = ?, result = ? WHERE event_id = ?",
                (iso(self._clock()), json.dumps(result), event_id),
            )

    def release(self, event_id: str) -> None:
        with connect(self.path) as conn:
            conn.execute("DELETE FROM event_ledger WHERE event_id = ? AND status = 'in_progress'", (event_id,))

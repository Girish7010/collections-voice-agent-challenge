"""Dead-letter queue: call events that still failed after retries, in SQLite.

Lifecycle of a row:
    pending --claim()--> replaying --(success)--> resolved
                             |
                             +--(fails again: add())--> pending
                             +--(lease expires)-------> claimable again

`claim()` is a single conditional UPDATE, so only one replayer can own a row
at a time, even across processes sharing the file.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass

from app.db import connect, init_db, iso


@dataclass(frozen=True)
class DeadLetter:
    event_id: str
    campaign_id: str | None
    payload: dict
    failure_reason: str
    attempts: int
    retry_count: int
    failure_count: int
    status: str
    first_failed_at: str
    last_failed_at: str
    replay_attempts: int
    last_replay_at: str | None
    resolved_at: str | None
    resolved_by: str | None

    def to_dict(self) -> dict:
        return self.__dict__.copy()


_COLUMNS = ", ".join(DeadLetter.__dataclass_fields__)


def _row(r) -> DeadLetter:
    d = dict(r)
    d["payload"] = json.loads(d["payload"])
    return DeadLetter(**d)


class DeadLetterQueue:
    def __init__(self, path: str, clock: Callable[[], float] = time.time):
        self.path = path
        self._clock = clock
        init_db(path)

    def add(self, event: dict, *, reason: str, attempts: int) -> None:
        """Insert, or update an existing row (e.g. a replay that failed again)."""
        now = iso(self._clock())
        with connect(self.path) as conn:
            conn.execute(
                """
                INSERT INTO dead_letters (event_id, campaign_id, payload, failure_reason, attempts,
                                          retry_count, first_failed_at, last_failed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET
                    failure_reason = excluded.failure_reason,
                    attempts       = excluded.attempts,
                    retry_count    = excluded.retry_count,
                    last_failed_at = excluded.last_failed_at,
                    failure_count  = failure_count + 1,
                    status         = 'pending',
                    claimed_at     = NULL,
                    resolved_at    = NULL,
                    resolved_by    = NULL
                """,
                (event["event_id"], event.get("campaign_id"), json.dumps(event), reason,
                 attempts, max(attempts - 1, 0), now, now),
            )

    def get(self, event_id: str) -> DeadLetter | None:
        with connect(self.path) as conn:
            r = conn.execute(f"SELECT {_COLUMNS} FROM dead_letters WHERE event_id = ?", (event_id,)).fetchone()
        return _row(r) if r else None

    def list(self, status: str | None = None, limit: int = 100, offset: int = 0) -> list[DeadLetter]:
        sql = f"SELECT {_COLUMNS} FROM dead_letters"
        args: list = []
        if status:
            sql += " WHERE status = ?"
            args.append(status)
        sql += " ORDER BY id LIMIT ? OFFSET ?"
        args += [limit, offset]
        with connect(self.path) as conn:
            return [_row(r) for r in conn.execute(sql, args).fetchall()]

    def depth(self) -> int:
        """Events still waiting for a successful replay (pending + being replayed)."""
        with connect(self.path) as conn:
            return conn.execute("SELECT COUNT(*) FROM dead_letters WHERE status != 'resolved'").fetchone()[0]

    def counts(self) -> dict[str, int]:
        with connect(self.path) as conn:
            rows = conn.execute("SELECT status, COUNT(*) FROM dead_letters GROUP BY status ORDER BY status").fetchall()
        return {status: n for status, n in rows}

    def claim(self, event_id: str, lease_s: float = 60.0) -> bool:
        now = self._clock()
        with connect(self.path) as conn:
            cur = conn.execute(
                """
                UPDATE dead_letters
                   SET status = 'replaying', claimed_at = ?, last_replay_at = ?,
                       replay_attempts = replay_attempts + 1
                 WHERE event_id = ?
                   AND (status = 'pending' OR (status = 'replaying' AND claimed_at < ?))
                """,
                (now, iso(now), event_id, now - lease_s),
            )
            return cur.rowcount == 1

    def unclaim(self, event_id: str) -> None:
        with connect(self.path) as conn:
            conn.execute(
                "UPDATE dead_letters SET status = 'pending', claimed_at = NULL WHERE event_id = ? AND status = 'replaying'",
                (event_id,),
            )

    def resolve(self, event_id: str, *, resolved_by: str) -> bool:
        with connect(self.path) as conn:
            cur = conn.execute(
                """
                UPDATE dead_letters SET status = 'resolved', resolved_at = ?, resolved_by = ?, claimed_at = NULL
                 WHERE event_id = ? AND status != 'resolved'
                """,
                (iso(self._clock()), resolved_by, event_id),
            )
            return cur.rowcount == 1

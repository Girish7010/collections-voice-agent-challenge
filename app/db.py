"""SQLite helpers shared by the DLQ and the idempotency ledger.

One short-lived connection per operation keeps things thread-safe (calls run in
a dedicated worker thread, see EventProcessor.db). WAL mode lets readers (e.g. the CLI or
`sqlite3` in a terminal) inspect the file while the service writes to it.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS dead_letters (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id         TEXT NOT NULL UNIQUE,
    campaign_id      TEXT,
    payload          TEXT NOT NULL,
    failure_reason   TEXT NOT NULL,
    attempts         INTEGER NOT NULL,
    retry_count      INTEGER NOT NULL,
    failure_count    INTEGER NOT NULL DEFAULT 1,
    status           TEXT NOT NULL DEFAULT 'pending',  -- pending | replaying | resolved
    first_failed_at  TEXT NOT NULL,
    last_failed_at   TEXT NOT NULL,
    replay_attempts  INTEGER NOT NULL DEFAULT 0,
    last_replay_at   TEXT,
    claimed_at       REAL,
    resolved_at      TEXT,
    resolved_by      TEXT                               -- replay | resubmission | already_processed
);
CREATE INDEX IF NOT EXISTS idx_dead_letters_status ON dead_letters(status);

CREATE TABLE IF NOT EXISTS event_ledger (
    event_id      TEXT PRIMARY KEY,
    status        TEXT NOT NULL,                        -- in_progress | succeeded
    claimed_at    REAL NOT NULL,
    completed_at  TEXT,
    result        TEXT
);
"""


@contextmanager
def connect(path: str):
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def init_db(path: str) -> None:
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="milliseconds")

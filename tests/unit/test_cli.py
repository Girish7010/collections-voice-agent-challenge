"""DLQ command-line tool (inspect without the service running)."""

import json

from app.cli import main
from app.dlq import DeadLetterQueue

EVENT = {"event_id": "evt-1", "campaign_id": "CAMP-2026-09-A", "borrower_bucket": "0-30 DPD",
         "timestamp": "2026-09-01T09:03:12Z", "outcome": "connected"}


def test_list_and_show_and_stats(tmp_path, capsys, monkeypatch):
    db = str(tmp_path / "cli.db")
    monkeypatch.setenv("APP_DB_PATH", db)
    DeadLetterQueue(db).add(EVENT, reason="downstream_timeout", attempts=3)

    assert main(["dlq", "list"]) == 0
    out = capsys.readouterr().out
    assert "evt-1" in out and "downstream_timeout" in out and "pending" in out

    assert main(["dlq", "show", "evt-1"]) == 0
    assert json.loads(capsys.readouterr().out)["retry_count"] == 2

    assert main(["dlq", "stats"]) == 0
    assert json.loads(capsys.readouterr().out) == {"depth": 1, "counts": {"pending": 1}}


def test_show_missing_returns_error_code(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "cli.db"))
    assert main(["dlq", "show", "nope"]) == 1

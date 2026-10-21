"""DLQ command-line tool. Works directly on the SQLite file (APP_DB_PATH).

    python -m app.cli dlq list [--status pending] [--limit 50]
    python -m app.cli dlq show EVENT_ID
    python -m app.cli dlq stats
    python -m app.cli dlq replay [--event-id ID ...] [--limit 100] [--concurrency 4]

`replay` calls the dependency at APP_DEPENDENCY_URL with the same retry,
circuit-breaker and idempotency rules as the service. It is safe to run while
the service is also running: rows are claimed before replay and the ledger
prevents double processing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from app.config import Settings
from app.dlq import DeadLetterQueue
from app.event_processor import replay_dead_letters
from app.wiring import build


def _print_table(rows) -> None:
    header = f"{'event_id':<38} {'campaign_id':<16} {'status':<10} {'reason':<20} {'retries':>7} {'fails':>5} {'replays':>7}  last_failed_at"
    print(header)
    print("-" * len(header))
    for r in rows:
        print(f"{r.event_id:<38} {(r.campaign_id or '-'):<16} {r.status:<10} {r.failure_reason:<20} "
              f"{r.retry_count:>7} {r.failure_count:>5} {r.replay_attempts:>7}  {r.last_failed_at}")


async def _replay(settings: Settings, args) -> dict:
    c = build(settings)
    try:
        return await replay_dead_letters(c.processor, c.dlq, event_ids=args.event_id or None,
                                         limit=args.limit, concurrency=args.concurrency)
    finally:
        await c.aclose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="Inspect and replay the dead-letter queue.")
    sub = parser.add_subparsers(dest="group", required=True)
    dlq = sub.add_parser("dlq").add_subparsers(dest="cmd", required=True)

    p_list = dlq.add_parser("list", help="list dead letters")
    p_list.add_argument("--status", choices=["pending", "replaying", "resolved"])
    p_list.add_argument("--limit", type=int, default=50)
    p_show = dlq.add_parser("show", help="show one dead letter as JSON")
    p_show.add_argument("event_id")
    dlq.add_parser("stats", help="depth and counts by status")
    p_replay = dlq.add_parser("replay", help="replay pending dead letters")
    p_replay.add_argument("--event-id", action="append", help="replay only this id (repeatable)")
    p_replay.add_argument("--limit", type=int, default=100)
    p_replay.add_argument("--concurrency", type=int, default=4)

    args = parser.parse_args(argv)
    settings = Settings.from_env()
    queue = DeadLetterQueue(settings.db_path)

    if args.cmd == "list":
        _print_table(queue.list(status=args.status, limit=args.limit))
    elif args.cmd == "show":
        row = queue.get(args.event_id)
        if row is None:
            print(f"{args.event_id}: not in dead-letter queue", file=sys.stderr)
            return 1
        print(json.dumps(row.to_dict(), indent=2))
    elif args.cmd == "stats":
        print(json.dumps({"depth": queue.depth(), "counts": queue.counts()}))
    elif args.cmd == "replay":
        print(json.dumps(asyncio.run(_replay(settings, args)), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

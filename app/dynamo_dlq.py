"""Dead-letter queue backed by DynamoDB, so several Fargate tasks share one view.

Same lifecycle and same method contract as app.dlq.DeadLetterQueue (see
app/stores.py). The SQLite version relies on a conditional UPDATE for exclusive
claims; here the equivalent is a ConditionExpression, which DynamoDB evaluates
atomically on a single item.

Layout:
    table   PK event_id
    GSI     status-seq-index: PK status, SK seq

`seq` is a nanosecond timestamp fixed at first insert, which gives list() the
insertion order that the SQLite AUTOINCREMENT id provided. It is preserved across
re-adds so a record that fails a replay keeps its place in the queue.

Statuses are a closed set, so list() and counts() query the index once per status
rather than scanning the table, and nothing needs a constant-value partition key.

Eventual consistency: a GetItem here is strongly consistent, but a global secondary
index cannot be, so list(), counts() and depth() may briefly miss a very recent
write. Correctness does not depend on them - claim() is a conditional write, so a
replayer acting on a stale listing still cannot double-process a record.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable

from botocore.exceptions import ClientError

from app.db import iso
from app.dlq import DeadLetter
from app.dynamo import is_condition_failure, plain_item, table, to_decimal

STATUS_INDEX = "status-seq-index"
STATUSES = ("pending", "replaying", "resolved")

# Attribute names are all aliased: 'status' and 'result' are DynamoDB reserved
# words, and aliasing the rest keeps every expression uniform.
_N = {
    "#eid": "event_id", "#st": "status", "#seq": "seq", "#cid": "campaign_id",
    "#pl": "payload", "#fr": "failure_reason", "#att": "attempts", "#rc": "retry_count",
    "#fc": "failure_count", "#ffa": "first_failed_at", "#lfa": "last_failed_at",
    "#ra": "replay_attempts", "#lra": "last_replay_at", "#ca": "claimed_at",
    "#rat": "resolved_at", "#rby": "resolved_by",
}


def _names(*keys: str) -> dict[str, str]:
    """ExpressionAttributeNames for exactly these aliases.

    DynamoDB rejects a request that declares a name it does not use, so each call
    builds its own map instead of sharing one.
    """
    return {k: _N[k] for k in keys}


def _to_dead_letter(item: dict) -> DeadLetter:
    d = plain_item(item) or {}
    return DeadLetter(
        event_id=d["event_id"],
        campaign_id=d.get("campaign_id"),
        payload=json.loads(d["payload"]),
        failure_reason=d["failure_reason"],
        attempts=int(d["attempts"]),
        retry_count=int(d["retry_count"]),
        failure_count=int(d.get("failure_count", 1)),
        status=d["status"],
        first_failed_at=d["first_failed_at"],
        last_failed_at=d["last_failed_at"],
        replay_attempts=int(d.get("replay_attempts", 0)),
        last_replay_at=d.get("last_replay_at"),
        resolved_at=d.get("resolved_at"),
        resolved_by=d.get("resolved_by"),
    )


class DynamoDeadLetterQueue:
    def __init__(
        self,
        table_name: str,
        *,
        region: str | None = None,
        endpoint_url: str | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.table_name = table_name
        self._clock = clock
        self._t = table(table_name, region=region, endpoint_url=endpoint_url)

    # --- writes ------------------------------------------------------------

    def add(self, event: dict, *, reason: str, attempts: int) -> None:
        now_iso = iso(self._clock())
        campaign_id = event.get("campaign_id")

        sets = [
            "#fr = :fr", "#att = :att", "#rc = :rc", "#lfa = :now", "#st = :pending",
            "#pl = :pl",
            # Preserved across re-adds: a failed replay keeps its original position
            # and its replay history.
            "#ffa = if_not_exists(#ffa, :now)",
            "#seq = if_not_exists(#seq, :seq)",
            "#ra = if_not_exists(#ra, :zero)",
        ]
        # A re-add clears the outcome of the previous attempt.
        removes = ["#ca", "#rat", "#rby"]
        keys = ["#fr", "#att", "#rc", "#lfa", "#st", "#pl", "#ffa", "#seq", "#ra",
                "#ca", "#rat", "#rby", "#fc", "#cid"]

        if campaign_id is None:
            removes.append("#cid")
        else:
            sets.append("#cid = :cid")

        values = {
            ":fr": reason, ":att": attempts, ":rc": max(attempts - 1, 0),
            ":now": now_iso, ":pending": "pending", ":pl": json.dumps(event),
            ":seq": time.time_ns(), ":zero": 0, ":one": 1,
        }
        if campaign_id is not None:
            values[":cid"] = campaign_id

        self._t.update_item(
            Key={"event_id": event["event_id"]},
            # ADD on a missing attribute starts from zero, so a new record lands
            # with failure_count = 1.
            UpdateExpression=f"SET {', '.join(sets)} ADD #fc :one REMOVE {', '.join(removes)}",
            ExpressionAttributeNames=_names(*keys),
            ExpressionAttributeValues=values,
        )

    def claim(self, event_id: str, lease_s: float = 60.0) -> bool:
        now = self._clock()
        try:
            self._t.update_item(
                Key={"event_id": event_id},
                UpdateExpression="SET #st = :replaying, #ca = :now, #lra = :now_iso ADD #ra :one",
                # Claimable when pending, or when a previous replayer's lease ran out.
                ConditionExpression=(
                    "attribute_exists(#eid) AND "
                    "(#st = :pending OR (#st = :replaying AND #ca < :cutoff))"
                ),
                ExpressionAttributeNames=_names("#eid", "#st", "#ca", "#lra", "#ra"),
                ExpressionAttributeValues={
                    ":replaying": "replaying", ":pending": "pending", ":now": to_decimal(now),
                    ":now_iso": iso(now), ":cutoff": to_decimal(now - lease_s), ":one": 1,
                },
            )
            return True
        except ClientError as exc:
            if is_condition_failure(exc):
                return False  # another replayer holds a live lease
            raise

    def unclaim(self, event_id: str) -> None:
        try:
            self._t.update_item(
                Key={"event_id": event_id},
                UpdateExpression="SET #st = :pending REMOVE #ca",
                ConditionExpression="attribute_exists(#eid) AND #st = :replaying",
                ExpressionAttributeNames=_names("#eid", "#st", "#ca"),
                ExpressionAttributeValues={":pending": "pending", ":replaying": "replaying"},
            )
        except ClientError as exc:
            if not is_condition_failure(exc):
                raise  # not 'replaying': nothing to undo, same as the SQLite no-op

    def resolve(self, event_id: str, *, resolved_by: str) -> bool:
        try:
            self._t.update_item(
                Key={"event_id": event_id},
                UpdateExpression="SET #st = :resolved, #rat = :now_iso, #rby = :by REMOVE #ca",
                ConditionExpression="attribute_exists(#eid) AND #st <> :resolved",
                ExpressionAttributeNames=_names("#eid", "#st", "#rat", "#rby", "#ca"),
                ExpressionAttributeValues={
                    ":resolved": "resolved", ":now_iso": iso(self._clock()), ":by": resolved_by,
                },
            )
            return True
        except ClientError as exc:
            if is_condition_failure(exc):
                return False  # unknown event_id, or already resolved
            raise

    # --- reads -------------------------------------------------------------

    def get(self, event_id: str) -> DeadLetter | None:
        resp = self._t.get_item(Key={"event_id": event_id}, ConsistentRead=True)
        item = resp.get("Item")
        return _to_dead_letter(item) if item else None

    def list(self, status: str | None = None, limit: int = 100, offset: int = 0) -> list[DeadLetter]:
        want = offset + limit
        if status is not None:
            items = self._by_status(status, want)
        else:
            # Closed status set, so a query per status beats a table scan. Merge on
            # seq to recover a single insertion-ordered sequence.
            items = []
            for s in STATUSES:
                items.extend(self._by_status(s, want))
            items.sort(key=lambda i: i["seq"])
        return [_to_dead_letter(i) for i in items[offset:want]]

    def depth(self) -> int:
        return sum(self._count(s) for s in ("pending", "replaying"))

    def counts(self) -> dict[str, int]:
        # Statuses with no records are omitted, matching SQLite's GROUP BY.
        found = {s: self._count(s) for s in STATUSES}
        return {s: n for s, n in found.items() if n}

    # --- index helpers -----------------------------------------------------

    def _by_status(self, status: str, cap: int) -> list[dict]:
        items: list[dict] = []
        kwargs = {
            "IndexName": STATUS_INDEX,
            "KeyConditionExpression": "#st = :s",
            "ExpressionAttributeNames": _names("#st"),
            "ExpressionAttributeValues": {":s": status},
            "ScanIndexForward": True,  # ascending seq == insertion order
        }
        while len(items) < cap:
            resp = self._t.query(**kwargs, Limit=cap - len(items))
            items.extend(resp.get("Items", []))
            start = resp.get("LastEvaluatedKey")
            if not start:
                break
            kwargs["ExclusiveStartKey"] = start
        return items[:cap]

    def _count(self, status: str) -> int:
        total = 0
        kwargs = {
            "IndexName": STATUS_INDEX,
            "KeyConditionExpression": "#st = :s",
            "ExpressionAttributeNames": _names("#st"),
            "ExpressionAttributeValues": {":s": status},
            "Select": "COUNT",
        }
        while True:
            resp = self._t.query(**kwargs)
            total += resp.get("Count", 0)
            start = resp.get("LastEvaluatedKey")
            if not start:
                return total
            kwargs["ExclusiveStartKey"] = start

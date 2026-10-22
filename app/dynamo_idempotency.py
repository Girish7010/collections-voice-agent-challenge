"""Idempotency ledger backed by DynamoDB, shared by every task.

Same contract as app.idempotency.EventLedger (see app/stores.py). The SQLite
version wins a claim with INSERT OR IGNORE; here it is a PutItem guarded by
attribute_not_exists(event_id), which is the conditional put that
docs/architecture.md calls for. DynamoDB evaluates it atomically, so of any number
of concurrent callers exactly one gets CLAIMED.

TTL: complete() stamps expires_at so succeeded records age out instead of growing
without bound. This puts a horizon on the guarantee - an event replayed after its
record expires is treated as new and processed again. Keep ttl_days comfortably
longer than the longest window in which a duplicate could plausibly arrive, or set
it to 0 to keep records forever.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable

from botocore.exceptions import ClientError

from app.db import iso
from app.dynamo import is_condition_failure, plain_item, table, to_decimal
from app.idempotency import Claim, ClaimResult

_DAY_S = 86400


class DynamoEventLedger:
    def __init__(
        self,
        table_name: str,
        *,
        region: str | None = None,
        endpoint_url: str | None = None,
        clock: Callable[[], float] = time.time,
        ttl_days: int = 30,
    ):
        self.table_name = table_name
        self.ttl_days = ttl_days
        self._clock = clock
        self._t = table(table_name, region=region, endpoint_url=endpoint_url)

    def try_claim(self, event_id: str, lease_s: float = 30.0) -> Claim:
        now = self._clock()

        # The common path: nobody has this event_id yet.
        try:
            self._t.put_item(
                Item={"event_id": event_id, "status": "in_progress", "claimed_at": to_decimal(now)},
                ConditionExpression="attribute_not_exists(#eid)",
                ExpressionAttributeNames={"#eid": "event_id"},
            )
            return Claim(ClaimResult.CLAIMED)
        except ClientError as exc:
            if not is_condition_failure(exc):
                raise
            # A record exists: inspect it below.

        item = plain_item(self._t.get_item(Key={"event_id": event_id}, ConsistentRead=True).get("Item"))
        if item is None:
            # Released between the put and the read. Let the caller come back.
            return Claim(ClaimResult.IN_PROGRESS)

        if item["status"] == "succeeded":
            stored = json.loads(item["result"]) if item.get("result") else None
            return Claim(ClaimResult.DUPLICATE, stored)

        if item["claimed_at"] < now - lease_s:
            # The previous worker went away. Take the lease, but only if no one else
            # beat us to it: the condition pins the claimed_at we just read.
            try:
                self._t.update_item(
                    Key={"event_id": event_id},
                    UpdateExpression="SET #ca = :now",
                    ConditionExpression="#st = :inprog AND #ca = :old",
                    ExpressionAttributeNames={"#st": "status", "#ca": "claimed_at"},
                    ExpressionAttributeValues={
                        ":now": to_decimal(now),
                        ":inprog": "in_progress",
                        ":old": to_decimal(item["claimed_at"]),
                    },
                )
                return Claim(ClaimResult.CLAIMED)
            except ClientError as exc:
                if not is_condition_failure(exc):
                    raise

        return Claim(ClaimResult.IN_PROGRESS)

    def complete(self, event_id: str, result: dict) -> None:
        now = self._clock()
        sets = ["#st = :done", "#cat = :now_iso", "#res = :res"]
        names = {"#eid": "event_id", "#st": "status", "#cat": "completed_at", "#res": "result"}
        values = {":done": "succeeded", ":now_iso": iso(now), ":res": json.dumps(result)}

        if self.ttl_days > 0:
            sets.append("#exp = :exp")
            names["#exp"] = "expires_at"
            values[":exp"] = int(now) + self.ttl_days * _DAY_S

        try:
            self._t.update_item(
                Key={"event_id": event_id},
                UpdateExpression=f"SET {', '.join(sets)}",
                # Without this, UpdateItem would create a record for an event_id that
                # has already been released. SQLite's UPDATE is a no-op there.
                ConditionExpression="attribute_exists(#eid)",
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
            )
        except ClientError as exc:
            if not is_condition_failure(exc):
                raise

    def release(self, event_id: str) -> None:
        try:
            self._t.delete_item(
                Key={"event_id": event_id},
                # Never drops a succeeded record.
                ConditionExpression="#st = :inprog",
                ExpressionAttributeNames={"#st": "status"},
                ExpressionAttributeValues={":inprog": "in_progress"},
            )
        except ClientError as exc:
            if not is_condition_failure(exc):
                raise

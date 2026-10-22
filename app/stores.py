"""The storage contracts the event processor depends on.

Two implementations satisfy each protocol:

    DeadLetterStore   app.dlq.DeadLetterQueue          (SQLite, single process)
                      app.dynamo_dlq.DynamoDeadLetterQueue    (DynamoDB, shared)

    LedgerStore       app.idempotency.EventLedger      (SQLite, single process)
                      app.dynamo_idempotency.DynamoEventLedger (DynamoDB, shared)

These are runtime-checkable protocols rather than base classes so the SQLite
classes stay free of any AWS import, and so a test double only has to provide the
methods it uses.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.dlq import DeadLetter
from app.idempotency import Claim


@runtime_checkable
class DeadLetterStore(Protocol):
    def add(self, event: dict, *, reason: str, attempts: int) -> None:
        """Record a failure. Called again for the same event_id after a failed replay,
        which increments failure_count and returns the record to 'pending'."""

    def get(self, event_id: str) -> DeadLetter | None: ...

    def list(self, status: str | None = None, limit: int = 100, offset: int = 0) -> list[DeadLetter]:
        """Records in insertion order, optionally filtered to one status."""

    def depth(self) -> int:
        """Records not yet resolved: pending + replaying."""

    def counts(self) -> dict[str, int]: ...

    def claim(self, event_id: str, lease_s: float = 60.0) -> bool:
        """Take exclusive ownership for a replay. False if another replayer holds a
        live lease. Must be atomic: exactly one concurrent caller may win."""

    def unclaim(self, event_id: str) -> None:
        """Return a claimed record to 'pending'. A no-op unless it is 'replaying'."""

    def resolve(self, event_id: str, *, resolved_by: str) -> bool:
        """Mark resolved. False if unknown or already resolved."""


@runtime_checkable
class LedgerStore(Protocol):
    def try_claim(self, event_id: str, lease_s: float = 30.0) -> Claim:
        """Attempt exclusive ownership of an event_id. Must be atomic: of N
        concurrent callers exactly one gets CLAIMED."""

    def complete(self, event_id: str, result: dict) -> None:
        """Mark succeeded and store the result for duplicate replies."""

    def release(self, event_id: str) -> None:
        """Drop an in-progress claim after failure. Must never touch a succeeded row."""

"""Durable ledger of leaver deferred-deletion due dates.

The leaver flow computes a deletion due date but never deletes anything
itself (see flows/leaver.py docstring) — this module is where that due date
is *persisted*, so `deferred_deletion_sweep` (function_app.py) can read it
back and report which accounts are past due without trusting anything still
being in memory from when the leaver event was originally processed.

Backed by Azure Table Storage in Azure (PartitionKey = "LeaverSchedule",
RowKey = user_principal_name — the stricter UPN regex in models.py already
rejects the characters Table Storage forbids in a RowKey), with an in-memory
implementation for local dev and unit tests. Same pattern as idempotency.py.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Protocol

_PARTITION_KEY = "LeaverSchedule"


@dataclass(frozen=True)
class LeaverScheduleEntry:
    user_principal_name: str
    correlation_id: str
    deletion_due_date: date
    scheduled_at: datetime
    detail: str = ""


class LeaverScheduleStore(Protocol):
    def schedule(
        self, user_principal_name: str, correlation_id: str, due_date: date, detail: str = ""
    ) -> None: ...

    def list_due_on_or_before(self, cutoff: date) -> list[LeaverScheduleEntry]: ...

    def list_all(self) -> list[LeaverScheduleEntry]: ...


class InMemoryLeaverScheduleStore:
    """Thread-safe in-memory ledger. Used for local dev and unit tests."""

    def __init__(self) -> None:
        self._entries: dict[str, LeaverScheduleEntry] = {}
        self._lock = threading.Lock()

    def schedule(
        self, user_principal_name: str, correlation_id: str, due_date: date, detail: str = ""
    ) -> None:
        with self._lock:
            self._entries[user_principal_name] = LeaverScheduleEntry(
                user_principal_name=user_principal_name,
                correlation_id=correlation_id,
                deletion_due_date=due_date,
                scheduled_at=datetime.now(UTC),
                detail=detail,
            )

    def list_due_on_or_before(self, cutoff: date) -> list[LeaverScheduleEntry]:
        with self._lock:
            return [e for e in self._entries.values() if e.deletion_due_date <= cutoff]

    def list_all(self) -> list[LeaverScheduleEntry]:
        with self._lock:
            return list(self._entries.values())


class TableStorageLeaverScheduleStore:
    """Azure Table Storage-backed leaver deferred-deletion schedule. Uses the
    same storage account as AzureWebJobsStorage — no extra resource. Import of
    azure.data.tables is deferred to __init__ so this module stays importable
    (and the InMemory store usable) without the dependency present."""

    def __init__(self, connection_string: str, table_name: str = "LeaverSchedule") -> None:
        from azure.data.tables import TableServiceClient

        self._service = TableServiceClient.from_connection_string(connection_string)
        self._table = self._service.create_table_if_not_exists(table_name)

    def schedule(
        self, user_principal_name: str, correlation_id: str, due_date: date, detail: str = ""
    ) -> None:
        entity = {
            "PartitionKey": _PARTITION_KEY,
            "RowKey": user_principal_name,
            "correlation_id": correlation_id,
            "deletion_due_date": due_date.isoformat(),
            "scheduled_at": datetime.now(UTC).isoformat(),
            "detail": detail[:32000],
        }
        self._table.upsert_entity(entity)

    def list_due_on_or_before(self, cutoff: date) -> list[LeaverScheduleEntry]:
        return [e for e in self.list_all() if e.deletion_due_date <= cutoff]

    def list_all(self) -> list[LeaverScheduleEntry]:
        entities = self._table.query_entities(query_filter=f"PartitionKey eq '{_PARTITION_KEY}'")
        return [self._to_entry(e) for e in entities]

    @staticmethod
    def _to_entry(entity) -> LeaverScheduleEntry:
        return LeaverScheduleEntry(
            user_principal_name=entity["RowKey"],
            correlation_id=entity.get("correlation_id", ""),
            deletion_due_date=date.fromisoformat(entity["deletion_due_date"]),
            scheduled_at=datetime.fromisoformat(entity["scheduled_at"]),
            detail=entity.get("detail", ""),
        )

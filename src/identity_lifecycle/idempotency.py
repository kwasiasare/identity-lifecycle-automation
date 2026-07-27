"""Two layers of idempotency, both required for "safe to replay" semantics:

1. Mutation-level: every GraphClient write method checks current state before
   writing (see graph_client.py) — this is what makes replaying a whole event
   produce no duplicate side effects even if the ledger below were unavailable.

2. Event-level ledger: a lightweight dedupe record per (event_type, correlation_id)
   so a queue redelivery (Storage Queues are at-least-once) short-circuits before
   re-running Graph calls at all, and so the audit trail can distinguish "genuine
   replay, no-op" from "first run". Backed by Azure Table Storage in Azure,
   with an in-memory implementation for local dev and unit tests.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol


@dataclass(frozen=True)
class LedgerEntry:
    event_type: str
    correlation_id: str
    status: str  # "completed" | "failed"
    processed_at: datetime
    detail: str = ""


class IdempotencyStore(Protocol):
    def has_completed(self, event_type: str, correlation_id: str) -> bool: ...

    def mark_completed(
        self, event_type: str, correlation_id: str, detail: str = ""
    ) -> None: ...

    def mark_failed(
        self, event_type: str, correlation_id: str, detail: str = ""
    ) -> None: ...

    def get(self, event_type: str, correlation_id: str) -> LedgerEntry | None: ...


class InMemoryIdempotencyStore:
    """Thread-safe in-memory ledger. Used for local dev and all unit tests."""

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], LedgerEntry] = {}
        self._lock = threading.Lock()

    def _key(self, event_type: str, correlation_id: str) -> tuple[str, str]:
        return (event_type, correlation_id)

    def has_completed(self, event_type: str, correlation_id: str) -> bool:
        entry = self.get(event_type, correlation_id)
        return entry is not None and entry.status == "completed"

    def mark_completed(self, event_type: str, correlation_id: str, detail: str = "") -> None:
        with self._lock:
            self._entries[self._key(event_type, correlation_id)] = LedgerEntry(
                event_type=event_type,
                correlation_id=correlation_id,
                status="completed",
                processed_at=datetime.now(UTC),
                detail=detail,
            )

    def mark_failed(self, event_type: str, correlation_id: str, detail: str = "") -> None:
        with self._lock:
            self._entries[self._key(event_type, correlation_id)] = LedgerEntry(
                event_type=event_type,
                correlation_id=correlation_id,
                status="failed",
                processed_at=datetime.now(UTC),
                detail=detail,
            )

    def get(self, event_type: str, correlation_id: str) -> LedgerEntry | None:
        with self._lock:
            return self._entries.get(self._key(event_type, correlation_id))


class TableStorageIdempotencyStore:
    """Azure Table Storage-backed ledger for production use.

    PartitionKey = event_type, RowKey = correlation_id. Uses the same storage
    account as the Functions app (AzureWebJobsStorage) — no extra resource.
    Import of azure.data.tables is deferred to __init__ so this module stays
    importable (and the InMemory store usable) without the dependency present.
    """

    def __init__(self, connection_string: str, table_name: str = "IdempotencyLedger") -> None:
        from azure.data.tables import TableServiceClient

        self._service = TableServiceClient.from_connection_string(connection_string)
        self._table = self._service.create_table_if_not_exists(table_name)

    def has_completed(self, event_type: str, correlation_id: str) -> bool:
        entry = self.get(event_type, correlation_id)
        return entry is not None and entry.status == "completed"

    def mark_completed(self, event_type: str, correlation_id: str, detail: str = "") -> None:
        self._upsert(event_type, correlation_id, "completed", detail)

    def mark_failed(self, event_type: str, correlation_id: str, detail: str = "") -> None:
        self._upsert(event_type, correlation_id, "failed", detail)

    def _upsert(self, event_type: str, correlation_id: str, status: str, detail: str) -> None:
        entity = {
            "PartitionKey": event_type,
            "RowKey": correlation_id,
            "status": status,
            "detail": detail[:32000],
            "processedAt": datetime.now(UTC).isoformat(),
        }
        self._table.upsert_entity(entity)

    def get(self, event_type: str, correlation_id: str) -> LedgerEntry | None:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            entity = self._table.get_entity(partition_key=event_type, row_key=correlation_id)
        except ResourceNotFoundError:
            return None
        return LedgerEntry(
            event_type=event_type,
            correlation_id=correlation_id,
            status=entity["status"],
            processed_at=datetime.fromisoformat(entity["processedAt"]),
            detail=entity.get("detail", ""),
        )

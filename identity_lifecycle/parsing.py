"""Parses HTTP/blob intake payloads (CSV or JSON) into validated UserEvent objects.

Design goal: never let one bad row abort the whole batch. Each row is validated
independently; failures are collected as ValidationIssue entries so the caller
(HTTP intake) can report a partial-success response, and valid events are still
enqueued.
"""

from __future__ import annotations

import csv
import io
import json
from typing import Any

from pydantic import ValidationError

from identity_lifecycle.models import EventType, ParsedBatch, UserEvent

# CSV columns map 1:1 to UserEvent field names; unknown columns are preserved in `raw`.
_KNOWN_FIELDS = set(UserEvent.model_fields.keys())


def parse_csv_bytes(data: bytes, *, default_source: str = "csv") -> ParsedBatch:
    """Parses a CSV file (header row required) into a ParsedBatch.

    Expected header includes at minimum: event_type, user_principal_name.
    Blank cells become None. Empty rows are skipped.
    """
    text = data.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    batch = ParsedBatch()
    for idx, row in enumerate(reader):
        if not any((v or "").strip() for v in row.values()):
            continue  # skip fully blank rows
        _parse_row(row, idx, batch, default_source)
    return batch


def parse_json_bytes(data: bytes, *, default_source: str = "http") -> ParsedBatch:
    """Parses a JSON payload — either a single event object or a list of them."""
    batch = ParsedBatch()
    try:
        payload = json.loads(data.decode("utf-8"))
    except json.JSONDecodeError as exc:
        batch.issues.append(
            _issue(0, f"invalid JSON payload: {exc}", {})
        )
        return batch

    rows = payload if isinstance(payload, list) else [payload]
    for idx, row in enumerate(rows):
        if not isinstance(row, dict):
            batch.issues.append(_issue(idx, "row is not a JSON object", {"value": row}))
            continue
        _parse_row(row, idx, batch, default_source)
    return batch


def _parse_row(
    row: dict[str, Any], idx: int, batch: ParsedBatch, default_source: str
) -> None:
    cleaned: dict[str, Any] = {}
    extras: dict[str, Any] = {}
    for key, value in row.items():
        norm_key = key.strip().lower().replace(" ", "_") if isinstance(key, str) else key
        value = value.strip() if isinstance(value, str) else value
        if value == "":
            value = None
        if norm_key in _KNOWN_FIELDS:
            cleaned[norm_key] = value
        else:
            extras[key] = value

    cleaned.setdefault("source", default_source)
    cleaned["raw"] = {**extras, **dict(row.items())}

    event_type_raw = cleaned.get("event_type")
    if isinstance(event_type_raw, str):
        cleaned["event_type"] = event_type_raw.strip().lower()

    try:
        event = UserEvent.model_validate(cleaned)
    except ValidationError as exc:
        batch.issues.append(_issue(idx, _format_pydantic_error(exc), row))
        return

    batch.events.append(event)


def _format_pydantic_error(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"])
        parts.append(f"{loc}: {err['msg']}")
    return "; ".join(parts)


def _issue(idx: int, message: str, raw_row: dict[str, Any]):
    from identity_lifecycle.models import ValidationIssue

    return ValidationIssue(row_index=idx, message=message, raw_row=raw_row)


def known_event_types() -> set[str]:
    return {e.value for e in EventType}

"""Event schemas for joiner/mover/leaver HR events.

These models are the contract between the intake layer (HTTP / blob CSV drop)
and the queue-triggered processor. Every event carries a `correlation_id` that
is propagated through Graph calls and audit records purely as a *trace* id —
it is NOT used to key the idempotency ledger (see `idempotency_key` below),
because `correlation_id` auto-generates a fresh uuid4 on every parse when the
source payload doesn't supply one, which would defeat dedup on replay of an
identical CSV/blob row.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import UTC, date, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

# Rejects '/', '\\', '?', '#', '%', and whitespace in either the local part or
# domain — those are exactly the characters that let a crafted UPN break out
# of a Graph REST path segment (e.g. "a@b.com/../../groups") or smuggle a
# query string / fragment into the URL. Defense in depth: every Graph call
# site additionally URL-encodes the UPN/id (see graph_client.py), but
# rejecting the shape at intake means a malformed value never even reaches
# a flow.
_UPN_RE = re.compile(r"^[^@/\\?#%\s]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")

# Extra CSV/JSON columns beyond the typed UserEvent fields are dropped by
# default rather than captured verbatim into `raw` — HR exports can contain
# arbitrary sensitive columns (salary, national ID, etc.) that this system has
# no business persisting, logging, or echoing back in an HTTP response. Only
# columns explicitly added here are preserved, and only for operational
# traceability (e.g. correlating an event back to a cost centre).
ALLOWED_RAW_FIELDS: frozenset[str] = frozenset({"cost_center"})


class EventType(str, Enum):
    JOINER = "joiner"
    MOVER = "mover"
    LEAVER = "leaver"


class UserEvent(BaseModel):
    """A single HR lifecycle event for one user.

    Field usage differs slightly by event_type:
      - joiner: display_name, department, job_title, manager_upn, employee_id
        should be populated; user_principal_name is the *target* UPN to create.
      - mover: user_principal_name identifies the existing user; new_department /
        new_job_title / new_manager_upn describe the change.
      - leaver: user_principal_name identifies the existing user; last_day_of_work
        drives the deferred-deletion schedule.
    """

    model_config = {"str_strip_whitespace": True}

    event_type: EventType
    user_principal_name: str
    correlation_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    # Idempotency ledger dedupe key — never a random default. Populated by
    # _set_idempotency_key below: the caller-supplied correlation_id if one
    # was explicitly provided (the caller controls its own dedupe token,
    # a la Stripe's Idempotency-Key), otherwise a deterministic hash of the
    # event's content so replaying the *same* CSV/blob row (which gets a
    # fresh random correlation_id every parse) still dedupes correctly.
    idempotency_key: str = ""
    source: str = "unspecified"
    employee_id: str | None = None
    display_name: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    personal_email: str | None = None

    # Joiner-specific
    department: str | None = None
    job_title: str | None = None
    manager_upn: str | None = None
    start_date: date | None = None

    # Mover-specific
    new_department: str | None = None
    new_job_title: str | None = None
    new_manager_upn: str | None = None
    effective_date: date | None = None

    # Leaver-specific
    last_day_of_work: date | None = None

    raw: dict[str, Any] = Field(default_factory=dict)
    received_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC)
    )

    @field_validator("user_principal_name")
    @classmethod
    def _upn_not_empty(cls, v: str) -> str:
        v = (v or "").lower()
        if not _UPN_RE.match(v):
            raise ValueError(
                f"user_principal_name must be a valid UPN (no '/', '\\\\', '?', '#', "
                f"'%', or whitespace), got: {v!r}"
            )
        return v

    @field_validator("manager_upn", "new_manager_upn")
    @classmethod
    def _optional_upn_lower(cls, v: str | None) -> str | None:
        if not v:
            return v
        v = v.lower()
        if not _UPN_RE.match(v):
            raise ValueError(
                f"manager UPN must be a valid UPN (no '/', '\\\\', '?', '#', "
                f"'%', or whitespace), got: {v!r}"
            )
        return v

    @field_validator("personal_email")
    @classmethod
    def _basic_email_shape(cls, v: str | None) -> str | None:
        if v and ("@" not in v or "." not in v.split("@")[-1]):
            raise ValueError(f"personal_email does not look like an email: {v!r}")
        return v

    @field_validator("raw")
    @classmethod
    def _cap_raw_to_allow_listed_fields(cls, v: dict[str, Any]) -> dict[str, Any]:
        """Defense in depth even if a caller constructs UserEvent directly
        (bypassing parsing.py's own allow-list filtering) — never let
        arbitrary HR columns ride along in `raw`."""
        return {k: val for k, val in v.items() if k in ALLOWED_RAW_FIELDS}

    @model_validator(mode="after")
    def _set_idempotency_key(self) -> UserEvent:
        if self.idempotency_key:
            return self  # already set (e.g. round-tripped from a queue message)
        if "correlation_id" in self.model_fields_set:
            # Caller explicitly supplied a correlation_id — treat it as their
            # own dedupe token (their choice, their responsibility).
            self.idempotency_key = self.correlation_id
        else:
            self.idempotency_key = compute_content_hash_key(self)
        return self


def compute_content_hash_key(event: UserEvent) -> str:
    """Deterministic idempotency key derived from event content, used when no
    correlation_id was explicitly supplied by the caller — so replaying the
    exact same CSV/blob row (which gets a fresh random correlation_id every
    parse) still dedupes against the idempotency ledger."""
    relevant_date = event.effective_date or event.start_date or event.last_day_of_work
    date_str = relevant_date.isoformat() if relevant_date else ""
    row_repr = json.dumps(event.raw, sort_keys=True, default=str)
    row_hash = hashlib.sha256(row_repr.encode("utf-8")).hexdigest()[:16]
    material = "|".join(
        [event.event_type.value, event.user_principal_name, date_str, event.source, row_hash]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class ValidationIssue(BaseModel):
    row_index: int
    message: str
    # Deliberately NOT the raw row values (may contain arbitrary/sensitive HR
    # columns) — just which column names were present, enough to debug a
    # malformed row without echoing its content back in an HTTP response or a
    # log line.
    field_names: list[str] = Field(default_factory=list)


class ParsedBatch(BaseModel):
    """Result of parsing an intake payload (CSV or JSON) into events."""

    events: list[UserEvent] = Field(default_factory=list)
    issues: list[ValidationIssue] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return len(self.issues) == 0

    @property
    def event_count(self) -> int:
        return len(self.events)

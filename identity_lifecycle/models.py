"""Event schemas for joiner/mover/leaver HR events.

These models are the contract between the intake layer (HTTP / blob CSV drop)
and the queue-triggered processor. Every event carries a `correlation_id` that
is propagated through Graph calls and audit records so a single HR event can
be traced end-to-end in Log Analytics.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator


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
        if not v or "@" not in v:
            raise ValueError(
                f"user_principal_name must be a valid UPN, got: {v!r}"
            )
        return v.lower()

    @field_validator("manager_upn", "new_manager_upn")
    @classmethod
    def _optional_upn_lower(cls, v: str | None) -> str | None:
        return v.lower() if v else v

    @field_validator("personal_email")
    @classmethod
    def _basic_email_shape(cls, v: str | None) -> str | None:
        if v and ("@" not in v or "." not in v.split("@")[-1]):
            raise ValueError(f"personal_email does not look like an email: {v!r}")
        return v


class ValidationIssue(BaseModel):
    row_index: int
    message: str
    raw_row: dict[str, Any] = Field(default_factory=dict)


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

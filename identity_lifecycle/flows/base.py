"""Shared result type + helpers for flow implementations."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

import httpx

from identity_lifecycle.graph_client import GraphApiError


class StepResult(str, Enum):
    SUCCESS = "success"  # a mutation happened
    SKIPPED = "skipped"  # already in desired state — idempotency in action
    FAILED = "failed"
    INFO = "info"  # informational/audit-only record — not a Graph mutation


@dataclass
class FlowStep:
    action: str
    target: str
    result: StepResult
    detail: str = ""
    # Only meaningful when result == FAILED. True means "this looked like a
    # transient condition (throttling, timeout, 5xx) — retrying the message
    # might succeed"; False means "this is a data/config problem (UPN not
    # found, group missing) — retrying the identical message will fail the
    # same way every time". queue_processor uses this to decide whether to
    # raise (let the host retry/poison) or record a permanent failure and
    # stop.
    retryable: bool = False


@dataclass
class FlowOutcome:
    """Aggregate result of running a flow against one event."""

    event_type: str
    correlation_id: str
    user_principal_name: str
    steps: list[FlowStep] = field(default_factory=list)

    def add(
        self,
        action: str,
        target: str,
        result: StepResult,
        detail: str = "",
        *,
        retryable: bool = False,
    ) -> FlowStep:
        step = FlowStep(action=action, target=target, result=result, detail=detail, retryable=retryable)
        self.steps.append(step)
        return step

    @property
    def succeeded(self) -> bool:
        return all(s.result != StepResult.FAILED for s in self.steps)

    @property
    def has_transient_failure(self) -> bool:
        """True if any FAILED step looked transient — queue_processor should
        raise so the host retries the message instead of recording a
        permanent failure."""
        return any(s.result == StepResult.FAILED and s.retryable for s in self.steps)

    @property
    def changed(self) -> bool:
        return any(s.result == StepResult.SUCCESS for s in self.steps)

    def summary(self) -> str:
        counts: dict[StepResult, int] = {}
        for s in self.steps:
            counts[s.result] = counts.get(s.result, 0) + 1
        return ", ".join(f"{k.value}={v}" for k, v in counts.items())


def is_transient_graph_failure(exc: Exception) -> bool:
    """Classifies an exception raised by a GraphClient call as transient
    (throttling/timeout/server error — worth retrying) or permanent (a 4xx
    business/data problem that will fail identically on retry).

    GraphClient already retries 429/503 internally with bounded backoff (see
    graph_client.py), so an exception reaching a flow means those retries
    were exhausted or a different error occurred — a persistent 429/5xx is
    still worth surfacing as transient so the *message* gets another attempt
    (with a fresh retry budget) rather than being marked permanently failed.
    """
    if isinstance(exc, GraphApiError):
        return exc.status_code == 429 or exc.status_code >= 500
    return isinstance(exc, httpx.TransportError)

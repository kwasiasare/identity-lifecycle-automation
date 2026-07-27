"""Shared result type + helpers for flow implementations."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


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


@dataclass
class FlowOutcome:
    """Aggregate result of running a flow against one event."""

    event_type: str
    correlation_id: str
    user_principal_name: str
    steps: list[FlowStep] = field(default_factory=list)

    def add(self, action: str, target: str, result: StepResult, detail: str = "") -> FlowStep:
        step = FlowStep(action=action, target=target, result=result, detail=detail)
        self.steps.append(step)
        return step

    @property
    def succeeded(self) -> bool:
        return all(s.result != StepResult.FAILED for s in self.steps)

    @property
    def changed(self) -> bool:
        return any(s.result == StepResult.SUCCESS for s in self.steps)

    def summary(self) -> str:
        counts: dict[StepResult, int] = {}
        for s in self.steps:
            counts[s.result] = counts.get(s.result, 0) + 1
        return ", ".join(f"{k.value}={v}" for k, v in counts.items())

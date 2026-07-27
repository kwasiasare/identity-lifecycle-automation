"""Leaver flow: HR "termination" event -> account disable, access removal, device retire.

Steps:
  1. Disable the account (idempotent — checks accountEnabled first).
  2. Revoke all refresh/session tokens. Graph's revokeSignInSessions is itself
     idempotent (revoking an already-revoked session set is a harmless no-op
     server-side), so this is always called and always logged `success`.
  3. Remove the user from every automation-managed group (same reconciliation
     approach as the mover flow, with an empty desired set).
  4. Retire every Intune-managed device owned by the user (idempotent — skips
     devices already pending/retired).
  5. Flag the mailbox for shared-mailbox conversion. Exchange Online mailbox
     operations are **not** reachable via Graph with application permissions
     in the same way directory objects are — this is deliberately isolated to
     a single EXO PowerShell module (exo/Convert-LeaverMailbox.psm1) run as a
     documented manual step or from an Azure Automation runbook in v1. This
     flow only records the audit intent; it never calls EXO itself.
  6. Schedule a 30-day (configurable) deferred deletion. This flow *never*
     deletes the account itself — it only computes and audits the deletion
     due-date. Actual deletion is a separate, explicitly-gated timer function
     (see function_app.py: `deferred_deletion_sweep`) that requires
     `settings.dry_run is False` and an explicit confirmation before it would
     ever call Graph's delete endpoint — a deliberate safety rail against a
     bad HR feed silently deleting accounts.
"""

from __future__ import annotations

from datetime import date, timedelta

from identity_lifecycle.audit import AuditLogger
from identity_lifecycle.config import Settings, all_managed_group_names
from identity_lifecycle.flows.base import FlowOutcome, StepResult
from identity_lifecycle.graph_client import GraphClient
from identity_lifecycle.models import UserEvent


def compute_deletion_due_date(event: UserEvent, settings: Settings, *, today: date | None = None) -> date:
    anchor = event.last_day_of_work or today or date.today()
    return anchor + timedelta(days=settings.leaver_deferred_delete_days)


def run_leaver(
    event: UserEvent,
    graph: GraphClient,
    audit: AuditLogger,
    settings: Settings,
) -> FlowOutcome:
    outcome = FlowOutcome(
        event_type=event.event_type.value,
        correlation_id=event.correlation_id,
        user_principal_name=event.user_principal_name,
    )

    user = graph.get_user_by_upn(event.user_principal_name)
    if user is None:
        outcome.add(
            "lookup_user",
            event.user_principal_name,
            StepResult.FAILED,
            detail="user not found — cannot process leaver event",
        )
        audit.record(
            correlation_id=event.correlation_id,
            event_type=outcome.event_type,
            action="lookup_user",
            target=event.user_principal_name,
            result="failed",
            detail="user not found — cannot process leaver event",
        )
        return outcome
    user_id = user["id"]

    _disable_account(event, graph, audit, outcome, user_id)
    _revoke_sessions(event, graph, audit, outcome, user_id)
    _remove_all_group_memberships(event, graph, audit, outcome, settings, user_id)
    _retire_devices(event, graph, audit, outcome, user_id)
    _flag_mailbox_conversion(event, audit, outcome)
    _schedule_deferred_deletion(event, audit, outcome, settings)

    return outcome


def _disable_account(
    event: UserEvent, graph: GraphClient, audit: AuditLogger, outcome: FlowOutcome, user_id: str
) -> None:
    changed = graph.ensure_account_disabled(user_id)
    result = StepResult.SUCCESS if changed else StepResult.SKIPPED
    detail = "account disabled" if changed else "account already disabled"
    outcome.add("disable_account", event.user_principal_name, result, detail=detail)
    audit.record(
        correlation_id=event.correlation_id,
        event_type=outcome.event_type,
        action="disable_account",
        target=event.user_principal_name,
        result=result.value,
        detail=detail,
    )


def _revoke_sessions(
    event: UserEvent, graph: GraphClient, audit: AuditLogger, outcome: FlowOutcome, user_id: str
) -> None:
    graph.revoke_sign_in_sessions(user_id)
    outcome.add(
        "revoke_sign_in_sessions",
        event.user_principal_name,
        StepResult.SUCCESS,
        detail="sign-in sessions revoked (idempotent — safe to repeat)",
    )
    audit.record(
        correlation_id=event.correlation_id,
        event_type=outcome.event_type,
        action="revoke_sign_in_sessions",
        target=event.user_principal_name,
        result="success",
        detail="sign-in sessions revoked (idempotent — safe to repeat)",
    )


def _remove_all_group_memberships(
    event: UserEvent,
    graph: GraphClient,
    audit: AuditLogger,
    outcome: FlowOutcome,
    settings: Settings,
    user_id: str,
) -> None:
    for group_name in sorted(all_managed_group_names(settings)):
        group = graph.get_group_by_name(group_name)
        if group is None:
            outcome.add(
                "remove_group_member",
                group_name,
                StepResult.FAILED,
                detail="group not found in directory",
            )
            continue
        changed = graph.ensure_group_member_removed(group["id"], user_id)
        result = StepResult.SUCCESS if changed else StepResult.SKIPPED
        detail = "removed" if changed else "was not a member"
        outcome.add("remove_group_member", group_name, result, detail=detail)
        audit.record(
            correlation_id=event.correlation_id,
            event_type=outcome.event_type,
            action="remove_group_member",
            target=group_name,
            result=result.value,
            detail=detail,
        )


def _retire_devices(
    event: UserEvent, graph: GraphClient, audit: AuditLogger, outcome: FlowOutcome, user_id: str
) -> None:
    devices = graph.list_owned_managed_devices(user_id)
    if not devices:
        outcome.add(
            "retire_devices",
            event.user_principal_name,
            StepResult.SKIPPED,
            detail="no Intune-managed devices found for this user",
        )
        return
    for device in devices:
        device_id = device.get("id")
        device_name = device.get("deviceName", device_id)
        changed = graph.ensure_device_retired(device_id)
        result = StepResult.SUCCESS if changed else StepResult.SKIPPED
        detail = "retire command issued" if changed else "already retiring/retired"
        outcome.add("retire_device", device_name, result, detail=detail)
        audit.record(
            correlation_id=event.correlation_id,
            event_type=outcome.event_type,
            action="retire_device",
            target=device_name,
            result=result.value,
            detail=detail,
            device_id=device_id,
        )


def _flag_mailbox_conversion(event: UserEvent, audit: AuditLogger, outcome: FlowOutcome) -> None:
    detail = (
        "Mailbox conversion to shared is out of Graph's application-permission "
        "reach and is isolated to exo/Convert-LeaverMailbox.psm1 — run manually "
        "or via an Azure Automation runbook against this UPN. Not executed by "
        "this function."
    )
    outcome.add(
        "flag_mailbox_conversion", event.user_principal_name, StepResult.INFO, detail=detail
    )
    audit.record(
        correlation_id=event.correlation_id,
        event_type=outcome.event_type,
        action="flag_mailbox_conversion",
        target=event.user_principal_name,
        result="info",
        detail=detail,
    )


def _schedule_deferred_deletion(
    event: UserEvent, audit: AuditLogger, outcome: FlowOutcome, settings: Settings
) -> None:
    due_date = compute_deletion_due_date(event, settings)
    detail = (
        f"account eligible for deletion on/after {due_date.isoformat()} "
        f"({settings.leaver_deferred_delete_days}-day deferred deletion policy); "
        "actual deletion performed only by the gated deferred_deletion_sweep timer function"
    )
    outcome.add(
        "schedule_deferred_deletion", event.user_principal_name, StepResult.INFO, detail=detail
    )
    audit.record(
        correlation_id=event.correlation_id,
        event_type=outcome.event_type,
        action="schedule_deferred_deletion",
        target=event.user_principal_name,
        result="info",
        detail=detail,
        deletion_due_date=due_date.isoformat(),
    )

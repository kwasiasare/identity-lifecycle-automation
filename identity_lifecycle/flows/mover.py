"""Mover flow: department/manager/title change -> group + licensing reconciliation.

Approach: rather than trying to infer the user's *previous* department from the
event (HR systems don't reliably supply it), the flow treats the full set of
groups referenced by *any* configured department mapping as "automation-managed"
and reconciles membership to exactly the groups the user's *new* department
maps to. Groups outside that managed set (e.g. manually-added project groups)
are left untouched. This makes the flow idempotent and safe to replay: running
it twice in a row with the same event computes the same target state and the
second run reports every group step as `skipped`.

Reconciliation deliberately performs every addition before any removal (never
interleaved alphabetically by group name) — the user should never pass
through a moment where they've lost their old department's access before
gaining the new one, even though additions and removals happen as separate
Graph calls within the same flow run.

Every group add/remove and attribute change is logged individually, plus one
extra "access_recertification_note" audit record summarising the change —
this is the artifact a manager/security reviewer would check during a periodic
access recertification.
"""

from __future__ import annotations

from identity_lifecycle.audit import AuditLogger
from identity_lifecycle.config import Settings, all_managed_group_names
from identity_lifecycle.flows.base import FlowOutcome, StepResult
from identity_lifecycle.flows.joiner import resolve_department_mapping
from identity_lifecycle.graph_client import GraphClient
from identity_lifecycle.models import UserEvent


def run_mover(
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
            detail="user not found — cannot process mover event",
        )
        audit.record(
            correlation_id=event.correlation_id,
            event_type=outcome.event_type,
            action="lookup_user",
            target=event.user_principal_name,
            result="failed",
            detail="user not found — cannot process mover event",
        )
        return outcome
    user_id = user["id"]

    _apply_attribute_changes(event, graph, audit, outcome, user_id)
    _apply_manager_change(event, graph, audit, outcome, user_id)
    _reconcile_groups(event, graph, audit, outcome, settings, user_id)
    _log_recertification_note(event, audit, outcome)

    return outcome


def _apply_attribute_changes(
    event: UserEvent, graph: GraphClient, audit: AuditLogger, outcome: FlowOutcome, user_id: str
) -> None:
    desired: dict[str, str] = {}
    if event.new_department:
        desired["department"] = event.new_department
    if event.new_job_title:
        desired["jobTitle"] = event.new_job_title
    if not desired:
        return
    changed = graph.ensure_user_attributes(user_id, desired)
    result = StepResult.SUCCESS if changed else StepResult.SKIPPED
    detail = f"updated attributes: {sorted(desired)}" if changed else "attributes already current"
    outcome.add("update_attributes", event.user_principal_name, result, detail=detail)
    audit.record(
        correlation_id=event.correlation_id,
        event_type=outcome.event_type,
        action="update_attributes",
        target=event.user_principal_name,
        result=result.value,
        detail=detail,
        desired=desired,
    )


def _apply_manager_change(
    event: UserEvent, graph: GraphClient, audit: AuditLogger, outcome: FlowOutcome, user_id: str
) -> None:
    if not event.new_manager_upn:
        return
    manager = graph.get_user_by_upn(event.new_manager_upn)
    if manager is None:
        outcome.add(
            "set_manager",
            event.new_manager_upn,
            StepResult.FAILED,
            detail="new manager UPN not found in directory",
        )
        audit.record(
            correlation_id=event.correlation_id,
            event_type=outcome.event_type,
            action="set_manager",
            target=event.new_manager_upn,
            result="failed",
            detail="new manager UPN not found in directory",
        )
        return
    changed = graph.ensure_manager_set(user_id, manager["id"])
    result = StepResult.SUCCESS if changed else StepResult.SKIPPED
    detail = f"manager set to {event.new_manager_upn}" if changed else "manager already current"
    outcome.add("set_manager", event.new_manager_upn, result, detail=detail)
    audit.record(
        correlation_id=event.correlation_id,
        event_type=outcome.event_type,
        action="set_manager",
        target=event.new_manager_upn,
        result=result.value,
        detail=detail,
    )


def _reconcile_groups(
    event: UserEvent,
    graph: GraphClient,
    audit: AuditLogger,
    outcome: FlowOutcome,
    settings: Settings,
    user_id: str,
) -> None:
    if not event.new_department:
        return

    mapping = resolve_department_mapping(settings, event.new_department)
    if mapping is None:
        outcome.add(
            "reconcile_groups",
            event.new_department,
            StepResult.FAILED,
            detail="no department mapping configured for new_department",
        )
        audit.record(
            correlation_id=event.correlation_id,
            event_type=outcome.event_type,
            action="reconcile_groups",
            target=event.new_department,
            result="failed",
            detail="no department mapping configured for new_department",
        )
        return

    desired_names = set(mapping.security_groups)
    if mapping.license_group:
        desired_names.add(mapping.license_group)
    managed_names = all_managed_group_names(settings)

    # One memberOf read for the whole event, reused for every managed group
    # below instead of one Graph call per candidate group.
    member_of_ids = graph.get_member_of_group_ids(user_id)

    resolved_groups: dict[str, dict] = {}
    for group_name in sorted(managed_names):
        group = graph.get_group_by_name(group_name)
        if group is None:
            outcome.add(
                "reconcile_group_member",
                group_name,
                StepResult.FAILED,
                detail="group not found in directory",
            )
            continue
        resolved_groups[group_name] = group

    # Additions before removals, always — regardless of alphabetical group
    # name order — so the user is never briefly left with neither the old
    # nor the new department's access mid-reconciliation.
    to_add = [name for name in resolved_groups if name in desired_names]
    to_remove = [name for name in resolved_groups if name not in desired_names]

    for group_name in to_add:
        group_id = resolved_groups[group_name]["id"]
        if group_id in member_of_ids:
            result, detail = StepResult.SKIPPED, "already in desired state"
        else:
            graph.ensure_group_member(group_id, user_id, member_of_ids=member_of_ids)
            member_of_ids = member_of_ids | {group_id}
            result, detail = StepResult.SUCCESS, "added — now in scope for this department"
        outcome.add("reconcile_group_member", group_name, result, detail=detail)
        audit.record(
            correlation_id=event.correlation_id,
            event_type=outcome.event_type,
            action="reconcile_group_member",
            target=group_name,
            result=result.value,
            detail=detail,
        )

    for group_name in to_remove:
        group_id = resolved_groups[group_name]["id"]
        if group_id not in member_of_ids:
            result, detail = StepResult.SKIPPED, "already in desired state"
        else:
            graph.ensure_group_member_removed(group_id, user_id, member_of_ids=member_of_ids)
            member_of_ids = member_of_ids - {group_id}
            result, detail = StepResult.SUCCESS, "removed — out of scope for new department"
        outcome.add("reconcile_group_member", group_name, result, detail=detail)
        audit.record(
            correlation_id=event.correlation_id,
            event_type=outcome.event_type,
            action="reconcile_group_member",
            target=group_name,
            result=result.value,
            detail=detail,
        )


def _log_recertification_note(
    event: UserEvent, audit: AuditLogger, outcome: FlowOutcome
) -> None:
    detail = (
        f"mover processed: department -> {event.new_department or '(unchanged)'}, "
        f"job_title -> {event.new_job_title or '(unchanged)'}, "
        f"manager -> {event.new_manager_upn or '(unchanged)'}, "
        f"effective_date={event.effective_date or 'unspecified'}"
    )
    outcome.add(
        "access_recertification_note", event.user_principal_name, StepResult.INFO, detail=detail
    )
    audit.record(
        correlation_id=event.correlation_id,
        event_type=outcome.event_type,
        action="access_recertification_note",
        target=event.user_principal_name,
        result="info",
        detail=detail,
    )

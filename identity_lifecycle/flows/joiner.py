"""Joiner flow: HR "new hire" event -> fully provisioned Entra/M365 user.

Steps (each individually idempotent via GraphClient's check-before-write):
  1. Create the user if it doesn't already exist (by UPN).
  2. Reconcile displayName/jobTitle/department/employeeId to the event's
     values — runs every time, including replays, so a replay after a
     partial failure (or a corrected re-send of the same event) self-heals
     any attribute drift instead of only ever setting them at creation time.
  3. Set manager reference, if provided.
  4. Add to department security group(s) and the group-based-licensing group.
  5. Issue a Temporary Access Pass for first sign-in (passwordless preferred).
  6. Send a welcome email with sign-in instructions to the new hire's personal
     address (if supplied), sent from a dedicated no-reply/service mailbox
     (`settings.welcome_mail_sender`) — never from the just-created user's own
     mailbox, which isn't provisioned yet and would fail sendMail with
     MailboxNotEnabledForRESTAPI. This is the only step that is *not*
     naturally idempotent at the Graph layer (Graph has no "has this mail
     already been sent" check), so it is gated on step 1's `created` flag:
     welcome mail is only sent the run that actually created the user, never
     on replay. A mail-send failure degrades this step to `failed` — it never
     aborts the rest of the flow, which has already completed by this point.

Re-sending the same joiner event is safe: on a second run every step observes
the target state already holds and reports `skipped`.
"""

from __future__ import annotations

import html
import re
import secrets
import string
from typing import Any

from identity_lifecycle.audit import AuditLogger
from identity_lifecycle.config import DepartmentMapping, Settings
from identity_lifecycle.flows.base import FlowOutcome, StepResult, is_transient_graph_failure
from identity_lifecycle.graph_client import GraphClient
from identity_lifecycle.models import UserEvent

# mailNickname must be a safe, narrow character set — Graph accepts a wider
# range but this keeps the value predictable and avoids surprises downstream
# (e.g. in systems that treat mailNickname as a filesystem-safe or URL-safe
# token). Anything outside this set is stripped, not rejected, so a UPN local
# part with e.g. an apostrophe still produces a usable nickname.
_MAIL_NICKNAME_DISALLOWED = re.compile(r"[^A-Za-z0-9._-]")


def generate_temp_password(length: int = 16) -> str:
    """Cryptographically random password meeting a typical Entra complexity policy."""
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(length))
        if (
            any(c.islower() for c in pw)
            and any(c.isupper() for c in pw)
            and any(c.isdigit() for c in pw)
            and any(c in "!@#$%^&*" for c in pw)
        ):
            return pw


def _mail_nickname(upn: str) -> str:
    local_part = upn.split("@", 1)[0]
    sanitized = _MAIL_NICKNAME_DISALLOWED.sub("", local_part)
    return sanitized or "user"


def resolve_department_mapping(
    settings: Settings, department: str | None
) -> DepartmentMapping | None:
    if not department:
        return None
    return settings.department_mappings.get(department.strip().lower())


def run_joiner(
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

    display_name = event.display_name or event.user_principal_name.split("@", 1)[0]
    create_payload = {
        "accountEnabled": True,
        "displayName": display_name,
        "mailNickname": _mail_nickname(event.user_principal_name),
        "userPrincipalName": event.user_principal_name,
        "passwordProfile": {
            "forceChangePasswordNextSignIn": True,
            "password": generate_temp_password(settings.joiner_default_password_length),
        },
        "usageLocation": settings.default_usage_location,
    }
    if event.job_title:
        create_payload["jobTitle"] = event.job_title
    if event.department:
        create_payload["department"] = event.department
    if event.employee_id:
        create_payload["employeeId"] = event.employee_id

    user, created = graph.ensure_user_exists(event.user_principal_name, create_payload)
    user_id = user["id"]
    outcome.add(
        "create_user",
        event.user_principal_name,
        StepResult.SUCCESS if created else StepResult.SKIPPED,
        detail="user created" if created else "user already existed",
    )
    audit.record(
        correlation_id=event.correlation_id,
        event_type=outcome.event_type,
        action="create_user",
        target=event.user_principal_name,
        result="success" if created else "skipped",
        detail="user created" if created else "user already existed",
        user_id=user_id,
    )

    _ensure_attributes_current(event, graph, audit, outcome, user_id, display_name)
    _set_manager(event, graph, audit, outcome, user_id)
    _apply_group_memberships(event, graph, audit, outcome, settings, user_id)
    tap_code = _issue_temporary_access_pass(event, graph, audit, outcome, user_id)

    if created:
        _send_welcome_mail(event, graph, audit, outcome, tap_code, settings)
    else:
        outcome.add(
            "send_welcome_mail",
            event.user_principal_name,
            StepResult.SKIPPED,
            detail="user pre-existed; welcome mail only sent on initial creation",
        )
        audit.record(
            correlation_id=event.correlation_id,
            event_type=outcome.event_type,
            action="send_welcome_mail",
            target=event.user_principal_name,
            result="skipped",
            detail="user pre-existed; welcome mail only sent on initial creation",
        )

    return outcome


def _ensure_attributes_current(
    event: UserEvent,
    graph: GraphClient,
    audit: AuditLogger,
    outcome: FlowOutcome,
    user_id: str,
    display_name: str,
) -> None:
    """Reconciles displayName/jobTitle/department/employeeId every run
    (including replays) — called unconditionally after ensure_user_exists so
    a replayed event self-heals attribute drift rather than only ever setting
    these at initial creation time."""
    desired: dict[str, Any] = {"displayName": display_name}
    if event.job_title:
        desired["jobTitle"] = event.job_title
    if event.department:
        desired["department"] = event.department
    if event.employee_id:
        desired["employeeId"] = event.employee_id

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


def _set_manager(
    event: UserEvent, graph: GraphClient, audit: AuditLogger, outcome: FlowOutcome, user_id: str
) -> None:
    if not event.manager_upn:
        return
    manager = graph.get_user_by_upn(event.manager_upn)
    if manager is None:
        outcome.add(
            "set_manager",
            event.manager_upn,
            StepResult.FAILED,
            detail="manager UPN not found in directory",
        )
        audit.record(
            correlation_id=event.correlation_id,
            event_type=outcome.event_type,
            action="set_manager",
            target=event.manager_upn,
            result="failed",
            detail="manager UPN not found in directory",
        )
        return
    changed = graph.ensure_manager_set(user_id, manager["id"])
    result = StepResult.SUCCESS if changed else StepResult.SKIPPED
    detail = f"manager set to {event.manager_upn}" if changed else "manager already set"
    outcome.add("set_manager", event.manager_upn, result, detail=detail)
    audit.record(
        correlation_id=event.correlation_id,
        event_type=outcome.event_type,
        action="set_manager",
        target=event.manager_upn,
        result=result.value,
        detail=detail,
    )


def _apply_group_memberships(
    event: UserEvent,
    graph: GraphClient,
    audit: AuditLogger,
    outcome: FlowOutcome,
    settings: Settings,
    user_id: str,
) -> None:
    mapping = resolve_department_mapping(settings, event.department)
    if mapping is None:
        outcome.add(
            "apply_group_memberships",
            event.department or "(none)",
            StepResult.SKIPPED,
            detail="no department mapping configured",
        )
        return

    target_group_names = list(mapping.security_groups)
    if mapping.license_group:
        target_group_names.append(mapping.license_group)

    # One memberOf read for the whole event, reused for every candidate group
    # below — see GraphClient.get_member_of_group_ids docstring.
    member_of_ids = graph.get_member_of_group_ids(user_id)

    for group_name in target_group_names:
        group = graph.get_group_by_name(group_name)
        if group is None:
            outcome.add(
                "add_group_member",
                group_name,
                StepResult.FAILED,
                detail="group not found in directory",
            )
            audit.record(
                correlation_id=event.correlation_id,
                event_type=outcome.event_type,
                action="add_group_member",
                target=group_name,
                result="failed",
                detail="group not found in directory",
            )
            continue
        changed = graph.ensure_group_member(group["id"], user_id, member_of_ids=member_of_ids)
        if changed:
            member_of_ids = member_of_ids | {group["id"]}
        result = StepResult.SUCCESS if changed else StepResult.SKIPPED
        detail = "added to group" if changed else "already a member"
        outcome.add("add_group_member", group_name, result, detail=detail)
        audit.record(
            correlation_id=event.correlation_id,
            event_type=outcome.event_type,
            action="add_group_member",
            target=group_name,
            result=result.value,
            detail=detail,
        )


def _issue_temporary_access_pass(
    event: UserEvent, graph: GraphClient, audit: AuditLogger, outcome: FlowOutcome, user_id: str
) -> str | None:
    method = graph.issue_temporary_access_pass(user_id)
    if method is None:
        outcome.add(
            "issue_temporary_access_pass",
            event.user_principal_name,
            StepResult.SKIPPED,
            detail="a usable temporary access pass already exists",
        )
        audit.record(
            correlation_id=event.correlation_id,
            event_type=outcome.event_type,
            action="issue_temporary_access_pass",
            target=event.user_principal_name,
            result="skipped",
            detail="a usable temporary access pass already exists",
        )
        return None
    outcome.add(
        "issue_temporary_access_pass",
        event.user_principal_name,
        StepResult.SUCCESS,
        detail="temporary access pass issued",
    )
    audit.record(
        correlation_id=event.correlation_id,
        event_type=outcome.event_type,
        action="issue_temporary_access_pass",
        target=event.user_principal_name,
        result="success",
        detail="temporary access pass issued",
    )
    return method.get("temporaryAccessPass")


def _send_welcome_mail(
    event: UserEvent,
    graph: GraphClient,
    audit: AuditLogger,
    outcome: FlowOutcome,
    tap_code: str | None,
    settings: Settings,
) -> None:
    if not event.personal_email:
        outcome.add(
            "send_welcome_mail",
            event.user_principal_name,
            StepResult.SKIPPED,
            detail="no personal_email supplied on the joiner event",
        )
        audit.record(
            correlation_id=event.correlation_id,
            event_type=outcome.event_type,
            action="send_welcome_mail",
            target=event.user_principal_name,
            result="skipped",
            detail="no personal_email supplied on the joiner event",
        )
        return

    if not settings.welcome_mail_sender:
        detail = "WELCOME_MAIL_SENDER is not configured; cannot send welcome mail"
        outcome.add("send_welcome_mail", event.user_principal_name, StepResult.FAILED, detail=detail)
        audit.record(
            correlation_id=event.correlation_id,
            event_type=outcome.event_type,
            action="send_welcome_mail",
            target=event.user_principal_name,
            result="failed",
            detail=detail,
        )
        return

    safe_display_name = html.escape(event.display_name or event.user_principal_name)
    safe_upn = html.escape(event.user_principal_name)
    body = f"<p>Welcome, {safe_display_name}!</p><p>Your work account is <b>{safe_upn}</b>.</p>"
    body += (
        f"<p>Use this Temporary Access Pass to sign in for the first time: "
        f"<b>{html.escape(tap_code)}</b></p>"
        if tap_code
        else "<p>Your manager will share your first sign-in credentials separately.</p>"
    )

    # A mail-send failure must never abort the flow — every step above has
    # already completed successfully by this point, so the account itself is
    # fully provisioned even if the welcome email doesn't go out. Distinguish
    # transient (Graph throttling/outage — worth retrying the message) from
    # permanent (bad recipient address, sender not licensed, etc.) so
    # queue_processor knows whether to let the host retry.
    try:
        graph.send_mail(
            sender=settings.welcome_mail_sender,
            subject="Welcome — your new account is ready",
            body_html=body,
            to_addresses=[event.personal_email],
        )
    except Exception as exc:  # noqa: BLE001 - degrade to a failed step, never raise
        retryable = is_transient_graph_failure(exc)
        detail = f"welcome mail send failed: {exc}"
        outcome.add(
            "send_welcome_mail",
            event.personal_email,
            StepResult.FAILED,
            detail=detail,
            retryable=retryable,
        )
        audit.record(
            correlation_id=event.correlation_id,
            event_type=outcome.event_type,
            action="send_welcome_mail",
            target=event.personal_email,
            result="failed",
            detail=detail,
        )
        return

    outcome.add(
        "send_welcome_mail",
        event.personal_email,
        StepResult.SUCCESS,
        detail="welcome mail sent",
    )
    audit.record(
        correlation_id=event.correlation_id,
        event_type=outcome.event_type,
        action="send_welcome_mail",
        target=event.personal_email,
        result="success",
        detail="welcome mail sent",
    )

from __future__ import annotations

from identity_lifecycle.flows.base import StepResult
from identity_lifecycle.flows.joiner import (
    generate_temp_password,
    resolve_department_mapping,
    run_joiner,
)
from identity_lifecycle.models import EventType, UserEvent


def _joiner_event(**overrides) -> UserEvent:
    fields = {
        "event_type": EventType.JOINER,
        "user_principal_name": "new.hire@contoso.onmicrosoft.com",
        "display_name": "New Hire",
        "department": "Engineering",
        "job_title": "Software Engineer",
        "manager_upn": "manager@contoso.onmicrosoft.com",
        "personal_email": "new.hire@example.com",
        "correlation_id": "corr-joiner-1",
    }
    fields.update(overrides)
    return UserEvent(**fields)


def test_generate_temp_password_meets_complexity():
    pw = generate_temp_password(16)
    assert len(pw) == 16
    assert any(c.islower() for c in pw)
    assert any(c.isupper() for c in pw)
    assert any(c.isdigit() for c in pw)
    assert any(c in "!@#$%^&*" for c in pw)


def test_resolve_department_mapping_case_insensitive(settings):
    mapping = resolve_department_mapping(settings, "engineering")
    assert mapping is not None
    assert mapping.department == "Engineering"


def test_resolve_department_mapping_unknown_department_returns_none(settings):
    assert resolve_department_mapping(settings, "Unknown Dept") is None


def test_joiner_creates_user_when_absent(fake_graph, audit, settings):
    fake_graph.seed_group("grp-engineering-all")
    fake_graph.seed_group("lic-m365-e5")
    fake_graph.seed_user("manager@contoso.onmicrosoft.com", displayName="Manager")

    event = _joiner_event()
    outcome = run_joiner(event, fake_graph, audit, settings)

    assert outcome.succeeded
    assert "new.hire@contoso.onmicrosoft.com" in fake_graph.users
    create_step = next(s for s in outcome.steps if s.action == "create_user")
    assert create_step.result == StepResult.SUCCESS


def test_joiner_adds_department_group_and_license_group(fake_graph, audit, settings):
    fake_graph.seed_group("grp-engineering-all")
    fake_graph.seed_group("lic-m365-e5")
    fake_graph.seed_user("manager@contoso.onmicrosoft.com")

    event = _joiner_event()
    run_joiner(event, fake_graph, audit, settings)

    user = fake_graph.users["new.hire@contoso.onmicrosoft.com"]
    eng_group = fake_graph.groups["grp-engineering-all"]
    lic_group = fake_graph.groups["lic-m365-e5"]
    assert fake_graph.is_group_member(eng_group["id"], user["id"])
    assert fake_graph.is_group_member(lic_group["id"], user["id"])


def test_joiner_sets_manager(fake_graph, audit, settings):
    fake_graph.seed_group("grp-engineering-all")
    fake_graph.seed_group("lic-m365-e5")
    manager = fake_graph.seed_user("manager@contoso.onmicrosoft.com")

    event = _joiner_event()
    run_joiner(event, fake_graph, audit, settings)

    user = fake_graph.users["new.hire@contoso.onmicrosoft.com"]
    assert fake_graph.get_manager_id(user["id"]) == manager["id"]


def test_joiner_issues_temporary_access_pass_and_sends_welcome_mail(fake_graph, audit, settings):
    fake_graph.seed_group("grp-engineering-all")
    fake_graph.seed_group("lic-m365-e5")
    fake_graph.seed_user("manager@contoso.onmicrosoft.com")

    event = _joiner_event()
    run_joiner(event, fake_graph, audit, settings)

    assert len(fake_graph.sent_mail) == 1
    assert fake_graph.sent_mail[0]["to"] == ["new.hire@example.com"]
    assert "TAP" in fake_graph.sent_mail[0]["body_html"]


def test_joiner_skips_welcome_mail_when_no_personal_email(fake_graph, audit, settings):
    fake_graph.seed_group("grp-engineering-all")
    fake_graph.seed_group("lic-m365-e5")

    event = _joiner_event(personal_email=None)
    outcome = run_joiner(event, fake_graph, audit, settings)

    assert len(fake_graph.sent_mail) == 0
    mail_step = next(s for s in outcome.steps if s.action == "send_welcome_mail")
    assert mail_step.result == StepResult.SKIPPED


def test_joiner_manager_not_found_is_a_failed_step_but_does_not_raise(fake_graph, audit, settings):
    fake_graph.seed_group("grp-engineering-all")
    fake_graph.seed_group("lic-m365-e5")
    # manager NOT seeded

    event = _joiner_event()
    outcome = run_joiner(event, fake_graph, audit, settings)

    assert not outcome.succeeded
    manager_step = next(s for s in outcome.steps if s.action == "set_manager")
    assert manager_step.result == StepResult.FAILED
    # user creation still happened despite the manager failure
    assert "new.hire@contoso.onmicrosoft.com" in fake_graph.users


def test_joiner_replay_is_fully_idempotent(fake_graph, audit, settings):
    """Re-sending the same joiner event twice must not cause a single extra
    Graph mutation, and must not send a second welcome email."""
    fake_graph.seed_group("grp-engineering-all")
    fake_graph.seed_group("lic-m365-e5")
    fake_graph.seed_user("manager@contoso.onmicrosoft.com")

    event = _joiner_event()

    first_outcome = run_joiner(event, fake_graph, audit, settings)
    mutations_after_first = list(fake_graph.mutation_calls)
    assert first_outcome.changed

    second_outcome = run_joiner(event, fake_graph, audit, settings)

    assert fake_graph.mutation_calls == mutations_after_first  # zero new mutations
    assert len(fake_graph.sent_mail) == 1  # still only one welcome email ever sent
    assert not second_outcome.changed
    assert all(s.result != StepResult.FAILED for s in second_outcome.steps)


def test_joiner_missing_department_mapping_is_skipped_not_failed(fake_graph, audit, settings):
    fake_graph.seed_user("manager@contoso.onmicrosoft.com")
    event = _joiner_event(department="Nonexistent Dept")

    outcome = run_joiner(event, fake_graph, audit, settings)

    group_step = next(s for s in outcome.steps if s.action == "apply_group_memberships")
    assert group_step.result == StepResult.SKIPPED

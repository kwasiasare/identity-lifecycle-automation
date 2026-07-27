from __future__ import annotations

from datetime import date

from identity_lifecycle.flows.base import StepResult
from identity_lifecycle.flows.leaver import compute_deletion_due_date, run_leaver
from identity_lifecycle.models import EventType, UserEvent


def _seed_all_groups(fake_graph):
    for name in (
        "grp-engineering-all",
        "lic-m365-e5",
        "grp-sales-all",
        "lic-m365-e3",
        "grp-finance-all",
        "grp-hr-all",
    ):
        fake_graph.seed_group(name)


def _leaver_event(**overrides) -> UserEvent:
    fields = {
        "event_type": EventType.LEAVER,
        "user_principal_name": "leaver@contoso.onmicrosoft.com",
        "last_day_of_work": date(2026, 8, 1),
        "correlation_id": "corr-leaver-1",
    }
    fields.update(overrides)
    return UserEvent(**fields)


def test_compute_deletion_due_date_uses_last_day_of_work(settings):
    event = _leaver_event(last_day_of_work=date(2026, 8, 1))
    due = compute_deletion_due_date(event, settings)
    assert due == date(2026, 8, 31)  # +30 days default


def test_compute_deletion_due_date_falls_back_to_today_when_missing(settings):
    event = _leaver_event(last_day_of_work=None)
    due = compute_deletion_due_date(event, settings, today=date(2026, 7, 27))
    assert due == date(2026, 8, 26)


def test_leaver_disables_account(fake_graph, audit, settings):
    _seed_all_groups(fake_graph)
    fake_graph.seed_user("leaver@contoso.onmicrosoft.com")

    outcome = run_leaver(_leaver_event(), fake_graph, audit, settings)

    assert outcome.succeeded
    user = fake_graph.users["leaver@contoso.onmicrosoft.com"]
    assert user["accountEnabled"] is False
    disable_step = next(s for s in outcome.steps if s.action == "disable_account")
    assert disable_step.result == StepResult.SUCCESS


def test_leaver_removes_all_managed_group_memberships(fake_graph, audit, settings):
    _seed_all_groups(fake_graph)
    user = fake_graph.seed_user("leaver@contoso.onmicrosoft.com")
    eng_group = fake_graph.groups["grp-engineering-all"]
    eng_lic = fake_graph.groups["lic-m365-e5"]
    fake_graph.ensure_group_member(eng_group["id"], user["id"])
    fake_graph.ensure_group_member(eng_lic["id"], user["id"])

    run_leaver(_leaver_event(), fake_graph, audit, settings)

    assert not fake_graph.is_group_member(eng_group["id"], user["id"])
    assert not fake_graph.is_group_member(eng_lic["id"], user["id"])


def test_leaver_retires_all_owned_devices(fake_graph, audit, settings):
    _seed_all_groups(fake_graph)
    user = fake_graph.seed_user("leaver@contoso.onmicrosoft.com")
    device = fake_graph.seed_device(user["id"], "LAPTOP-01")

    outcome = run_leaver(_leaver_event(), fake_graph, audit, settings)

    assert fake_graph.device_states[device["id"]] == "retirePending"
    retire_step = next(s for s in outcome.steps if s.action == "retire_device")
    assert retire_step.result == StepResult.SUCCESS


def test_leaver_no_devices_is_skipped_not_failed(fake_graph, audit, settings):
    _seed_all_groups(fake_graph)
    fake_graph.seed_user("leaver@contoso.onmicrosoft.com")

    outcome = run_leaver(_leaver_event(), fake_graph, audit, settings)

    retire_step = next(s for s in outcome.steps if s.action == "retire_devices")
    assert retire_step.result == StepResult.SKIPPED


def test_leaver_flags_mailbox_conversion_but_never_calls_exo(fake_graph, audit, settings):
    _seed_all_groups(fake_graph)
    fake_graph.seed_user("leaver@contoso.onmicrosoft.com")

    outcome = run_leaver(_leaver_event(), fake_graph, audit, settings)

    mailbox_step = next(s for s in outcome.steps if s.action == "flag_mailbox_conversion")
    assert mailbox_step.result == StepResult.INFO
    assert "exo/Convert-LeaverMailbox.psm1" in mailbox_step.detail
    # No method on the fake graph client related to mailbox/EXO was ever invoked —
    # by construction FakeGraphClient exposes no such method, so this is
    # structurally guaranteed, not just behaviorally.


def test_leaver_schedules_deferred_deletion_with_correct_due_date(fake_graph, audit, settings):
    _seed_all_groups(fake_graph)
    fake_graph.seed_user("leaver@contoso.onmicrosoft.com")

    outcome = run_leaver(_leaver_event(last_day_of_work=date(2026, 8, 1)), fake_graph, audit, settings)

    step = next(s for s in outcome.steps if s.action == "schedule_deferred_deletion")
    assert "2026-08-31" in step.detail


def test_leaver_user_not_found_is_a_single_failed_step(fake_graph, audit, settings):
    outcome = run_leaver(
        _leaver_event(user_principal_name="ghost@contoso.onmicrosoft.com"), fake_graph, audit, settings
    )
    assert not outcome.succeeded
    assert len(outcome.steps) == 1
    assert outcome.steps[0].action == "lookup_user"


def test_leaver_replay_only_re_executes_documented_non_idempotent_steps(fake_graph, audit, settings):
    """Second run must not disable-again, remove-again, or retire-again — those
    are genuinely idempotent. revoke_sign_in_sessions is documented as always
    re-invoked (Graph itself makes repeated revocation harmless)."""
    _seed_all_groups(fake_graph)
    user = fake_graph.seed_user("leaver@contoso.onmicrosoft.com")
    fake_graph.seed_device(user["id"], "LAPTOP-01")
    event = _leaver_event()

    run_leaver(event, fake_graph, audit, settings)
    mutations_after_first = list(fake_graph.mutation_calls)

    second_outcome = run_leaver(event, fake_graph, audit, settings)
    new_mutations = fake_graph.mutation_calls[len(mutations_after_first):]

    assert all(m.startswith("revoke_sessions:") for m in new_mutations)

    disable_step = next(s for s in second_outcome.steps if s.action == "disable_account")
    assert disable_step.result == StepResult.SKIPPED
    device_step = next(s for s in second_outcome.steps if s.action == "retire_device")
    assert device_step.result == StepResult.SKIPPED
    group_steps = [s for s in second_outcome.steps if s.action == "remove_group_member"]
    assert all(s.result == StepResult.SKIPPED for s in group_steps)

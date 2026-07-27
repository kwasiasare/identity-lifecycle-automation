from __future__ import annotations

from identity_lifecycle.flows.base import StepResult
from identity_lifecycle.flows.mover import run_mover
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


def _mover_event(**overrides) -> UserEvent:
    fields = {
        "event_type": EventType.MOVER,
        "user_principal_name": "mover@contoso.onmicrosoft.com",
        "new_department": "Sales",
        "correlation_id": "corr-mover-1",
    }
    fields.update(overrides)
    return UserEvent(**fields)


def test_mover_moves_user_between_department_groups(fake_graph, audit, settings):
    _seed_all_groups(fake_graph)
    user = fake_graph.seed_user("mover@contoso.onmicrosoft.com", department="Engineering")
    eng_group = fake_graph.groups["grp-engineering-all"]
    eng_lic = fake_graph.groups["lic-m365-e5"]
    fake_graph.ensure_group_member(eng_group["id"], user["id"])
    fake_graph.ensure_group_member(eng_lic["id"], user["id"])
    fake_graph.mutation_calls.clear()

    event = _mover_event(new_department="Sales")
    outcome = run_mover(event, fake_graph, audit, settings)

    assert outcome.succeeded
    sales_group = fake_graph.groups["grp-sales-all"]
    sales_lic = fake_graph.groups["lic-m365-e3"]
    assert fake_graph.is_group_member(sales_group["id"], user["id"])
    assert fake_graph.is_group_member(sales_lic["id"], user["id"])
    assert not fake_graph.is_group_member(eng_group["id"], user["id"])
    assert not fake_graph.is_group_member(eng_lic["id"], user["id"])


def test_mover_updates_department_and_job_title_attributes(fake_graph, audit, settings):
    _seed_all_groups(fake_graph)
    fake_graph.seed_user("mover@contoso.onmicrosoft.com", department="Engineering")

    event = _mover_event(new_department="Sales", new_job_title="Account Executive")
    run_mover(event, fake_graph, audit, settings)

    user = fake_graph.users["mover@contoso.onmicrosoft.com"]
    assert user["department"] == "Sales"
    assert user["jobTitle"] == "Account Executive"


def test_mover_updates_manager(fake_graph, audit, settings):
    _seed_all_groups(fake_graph)
    fake_graph.seed_user("mover@contoso.onmicrosoft.com")
    new_manager = fake_graph.seed_user("newmanager@contoso.onmicrosoft.com")

    event = _mover_event(new_manager_upn="newmanager@contoso.onmicrosoft.com")
    run_mover(event, fake_graph, audit, settings)

    user = fake_graph.users["mover@contoso.onmicrosoft.com"]
    assert fake_graph.get_manager_id(user["id"]) == new_manager["id"]


def test_mover_new_manager_not_found_fails_that_step_only(fake_graph, audit, settings):
    _seed_all_groups(fake_graph)
    fake_graph.seed_user("mover@contoso.onmicrosoft.com")

    event = _mover_event(new_manager_upn="ghost@contoso.onmicrosoft.com")
    outcome = run_mover(event, fake_graph, audit, settings)

    manager_step = next(s for s in outcome.steps if s.action == "set_manager")
    assert manager_step.result == StepResult.FAILED
    # group reconciliation still ran despite manager failure
    assert any(s.action == "reconcile_group_member" for s in outcome.steps)


def test_mover_user_not_found_is_a_single_failed_step(fake_graph, audit, settings):
    event = _mover_event(user_principal_name="ghost@contoso.onmicrosoft.com")

    outcome = run_mover(event, fake_graph, audit, settings)

    assert not outcome.succeeded
    assert len(outcome.steps) == 1
    assert outcome.steps[0].action == "lookup_user"


def test_mover_logs_recertification_note(fake_graph, audit, settings):
    _seed_all_groups(fake_graph)
    fake_graph.seed_user("mover@contoso.onmicrosoft.com")

    event = _mover_event()
    outcome = run_mover(event, fake_graph, audit, settings)

    note_step = next(s for s in outcome.steps if s.action == "access_recertification_note")
    assert "Sales" in note_step.detail


def test_mover_reconciliation_adds_before_it_removes(fake_graph, audit, settings):
    """Regression test: alphabetically, 'grp-engineering-all' (a removal, out
    of scope for Sales) sorts BEFORE 'grp-sales-all' (an addition, in scope
    for Sales) — the old accidental-alphabetical-order bug would have issued
    the removal first. Every addition must be issued before any removal,
    regardless of group name ordering."""
    _seed_all_groups(fake_graph)
    user = fake_graph.seed_user("mover@contoso.onmicrosoft.com", department="Engineering")
    eng_group = fake_graph.groups["grp-engineering-all"]
    eng_lic = fake_graph.groups["lic-m365-e5"]
    fake_graph.ensure_group_member(eng_group["id"], user["id"])
    fake_graph.ensure_group_member(eng_lic["id"], user["id"])
    fake_graph.mutation_calls.clear()

    event = _mover_event(new_department="Sales")
    run_mover(event, fake_graph, audit, settings)

    add_indices = [i for i, m in enumerate(fake_graph.mutation_calls) if m.startswith("add_member:")]
    remove_indices = [i for i, m in enumerate(fake_graph.mutation_calls) if m.startswith("remove_member:")]
    assert add_indices, "expected at least one add_member mutation"
    assert remove_indices, "expected at least one remove_member mutation"
    assert max(add_indices) < min(remove_indices), (
        f"an addition happened after a removal: {fake_graph.mutation_calls}"
    )


def test_mover_replay_is_fully_idempotent(fake_graph, audit, settings):
    _seed_all_groups(fake_graph)
    fake_graph.seed_user("mover@contoso.onmicrosoft.com")
    fake_graph.seed_user("newmanager@contoso.onmicrosoft.com")

    event = _mover_event(new_manager_upn="newmanager@contoso.onmicrosoft.com", new_job_title="AE")

    run_mover(event, fake_graph, audit, settings)
    mutations_after_first = list(fake_graph.mutation_calls)

    second_outcome = run_mover(event, fake_graph, audit, settings)

    assert fake_graph.mutation_calls == mutations_after_first
    assert not second_outcome.changed

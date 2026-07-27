"""Unit tests for function_app.py — both the pure intake helper functions and
the queue_processor/poison-queue trigger bodies, exercised with GraphClient
and the storage-backed stores monkeypatched out (no real Azure Functions host,
no real Graph/Table Storage connectivity)."""

from __future__ import annotations

import json

import azure.functions as func
import function_app
import pytest

from identity_lifecycle.config import Settings, default_department_mappings
from identity_lifecycle.idempotency import InMemoryIdempotencyStore
from identity_lifecycle.leaver_schedule import InMemoryLeaverScheduleStore
from identity_lifecycle.models import EventType, ParsedBatch, UserEvent, ValidationIssue
from tests.unit.fake_graph_client import FakeGraphClient


def test_parse_intake_body_json_by_default():
    body = json.dumps(
        {"event_type": "joiner", "user_principal_name": "a@contoso.onmicrosoft.com"}
    ).encode()
    batch = function_app.parse_intake_body(body, content_type="application/json")
    assert batch.ok
    assert batch.events[0].event_type == EventType.JOINER


def test_parse_intake_body_csv_by_content_type():
    body = b"event_type,user_principal_name\njoiner,a@contoso.onmicrosoft.com\n"
    batch = function_app.parse_intake_body(body, content_type="text/csv")
    assert batch.ok
    assert batch.event_count == 1


def test_parse_intake_body_csv_by_format_query_param():
    body = b"event_type,user_principal_name\njoiner,a@contoso.onmicrosoft.com\n"
    batch = function_app.parse_intake_body(body, content_type="text/plain", format_param="csv")
    assert batch.ok
    assert batch.event_count == 1


def test_events_to_queue_messages_serialises_each_event_as_json():
    event = UserEvent(event_type=EventType.JOINER, user_principal_name="a@contoso.onmicrosoft.com")
    batch = ParsedBatch(events=[event])
    messages = function_app.events_to_queue_messages(batch)
    assert len(messages) == 1
    decoded = json.loads(messages[0])
    assert decoded["user_principal_name"] == "a@contoso.onmicrosoft.com"


def test_events_to_queue_messages_empty_batch():
    assert function_app.events_to_queue_messages(ParsedBatch()) == []


def test_build_intake_response_all_valid_is_200():
    event = UserEvent(event_type=EventType.JOINER, user_principal_name="a@contoso.onmicrosoft.com")
    status, payload = function_app.build_intake_response(ParsedBatch(events=[event]))
    assert status == 200
    assert payload["accepted"] == 1
    assert payload["rejected"] == 0
    assert payload["correlation_ids"] == [event.correlation_id]


def test_build_intake_response_partial_failure_is_207():
    event = UserEvent(event_type=EventType.JOINER, user_principal_name="a@contoso.onmicrosoft.com")
    batch = ParsedBatch(events=[event], issues=[ValidationIssue(row_index=1, message="bad row")])
    status, payload = function_app.build_intake_response(batch)
    assert status == 207
    assert payload["accepted"] == 1
    assert payload["rejected"] == 1


def test_build_intake_response_zero_events_zero_issues_is_400():
    """An empty payload (nothing parsed, nothing rejected either) must not
    read as a bland 200 "success" — there's nothing to report success about."""
    status, payload = function_app.build_intake_response(ParsedBatch())
    assert status == 400
    assert payload["accepted"] == 0
    assert payload["rejected"] == 0


def test_build_intake_response_all_rows_invalid_is_400_not_207():
    batch = ParsedBatch(issues=[ValidationIssue(row_index=0, message="bad"), ValidationIssue(row_index=1, message="also bad")])
    status, payload = function_app.build_intake_response(batch)
    assert status == 400
    assert payload["accepted"] == 0
    assert payload["rejected"] == 2


def test_build_intake_response_never_echoes_raw_row_values():
    batch = ParsedBatch(issues=[ValidationIssue(row_index=0, message="bad", field_names=["salary", "ssn"])])
    _status, payload = function_app.build_intake_response(batch)
    dumped = json.dumps(payload)
    assert "field_names" in dumped
    assert "raw_row" not in dumped


# ---------------------------------------------------------------------------
# queue_processor / events_poison_queue — GraphClient and the storage-backed
# stores are monkeypatched to keep these hermetic.
# ---------------------------------------------------------------------------


class _FakeGraphClientContext:
    """Stands in for `with GraphClient(settings) as graph:` in function_app.py."""

    def __init__(self, fake: FakeGraphClient) -> None:
        self._fake = fake

    def __call__(self, _settings) -> _FakeGraphClientContext:
        return self

    def __enter__(self) -> FakeGraphClient:
        return self._fake

    def __exit__(self, *exc_info: object) -> bool:
        return False


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """Patches function_app's module-level dependencies with hermetic fakes
    and returns them for assertions."""
    settings = Settings(
        local_audit_log_path=str(tmp_path / "audit-fallback.log"),
        department_mappings=default_department_mappings(),
        welcome_mail_sender="no-reply@contoso.onmicrosoft.com",
    )
    fake_graph = FakeGraphClient()
    idempotency_store = InMemoryIdempotencyStore()
    leaver_schedule_store = InMemoryLeaverScheduleStore()

    monkeypatch.setattr(function_app, "get_settings", lambda: settings)
    monkeypatch.setattr(function_app, "GraphClient", _FakeGraphClientContext(fake_graph))
    monkeypatch.setattr(function_app, "_build_idempotency_store", lambda _settings: idempotency_store)
    monkeypatch.setattr(function_app, "_build_leaver_schedule_store", lambda _settings: leaver_schedule_store)

    class _Wired:
        pass

    w = _Wired()
    w.settings = settings
    w.fake_graph = fake_graph
    w.idempotency_store = idempotency_store
    w.leaver_schedule_store = leaver_schedule_store
    return w


def _queue_message(event: UserEvent | None = None, *, raw_body: bytes | None = None, msg_id: str = "msg-1") -> func.QueueMessage:
    body = raw_body if raw_body is not None else event.model_dump_json().encode()
    return func.QueueMessage(id=msg_id, body=body)


def test_queue_processor_dispatches_joiner_and_marks_completed(wired):
    wired.fake_graph.seed_group("grp-engineering-all")
    wired.fake_graph.seed_group("lic-m365-e5")
    event = UserEvent(
        event_type=EventType.JOINER,
        user_principal_name="new.hire@contoso.onmicrosoft.com",
        department="Engineering",
        correlation_id="corr-qp-1",
    )

    function_app.queue_processor(_queue_message(event))

    assert "new.hire@contoso.onmicrosoft.com" in wired.fake_graph.users
    assert wired.idempotency_store.has_completed("joiner", event.idempotency_key)


def test_queue_processor_short_circuits_on_replay(wired):
    wired.fake_graph.seed_group("grp-engineering-all")
    wired.fake_graph.seed_group("lic-m365-e5")
    event = UserEvent(
        event_type=EventType.JOINER,
        user_principal_name="new.hire@contoso.onmicrosoft.com",
        department="Engineering",
        correlation_id="corr-qp-2",
    )
    wired.idempotency_store.mark_completed("joiner", event.idempotency_key, detail="already done")

    function_app.queue_processor(_queue_message(event))

    # No user was created — the replay short-circuited before dispatch.
    assert "new.hire@contoso.onmicrosoft.com" not in wired.fake_graph.users


def test_queue_processor_permanent_failure_is_recorded_and_not_raised(wired):
    """A mover event for a user that doesn't exist is a permanent (data)
    failure — queue_processor must record it and return normally, not raise
    (raising would just burn through maxDequeueCount for no benefit)."""
    event = UserEvent(
        event_type=EventType.MOVER,
        user_principal_name="ghost@contoso.onmicrosoft.com",
        new_department="Sales",
        correlation_id="corr-qp-3",
    )

    function_app.queue_processor(_queue_message(event))  # must not raise

    entry = wired.idempotency_store.get("mover", event.idempotency_key)
    assert entry is not None
    assert entry.status == "failed"


def test_queue_processor_unparseable_message_raises(wired):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        function_app.queue_processor(_queue_message(raw_body=b"not json at all"))


def test_queue_processor_flow_exception_is_marked_failed_and_reraised(wired, monkeypatch):
    event = UserEvent(
        event_type=EventType.JOINER,
        user_principal_name="boom@contoso.onmicrosoft.com",
        department="Engineering",
        correlation_id="corr-qp-4",
    )

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated Graph outage")

    monkeypatch.setitem(function_app._FLOW_DISPATCH, EventType.JOINER, _boom)

    with pytest.raises(RuntimeError, match="simulated Graph outage"):
        function_app.queue_processor(_queue_message(event))

    entry = wired.idempotency_store.get("joiner", event.idempotency_key)
    assert entry is not None
    assert entry.status == "failed"


def test_queue_processor_leaver_persists_deletion_due_date(wired):
    wired.fake_graph.seed_user("leaver@contoso.onmicrosoft.com")
    event = UserEvent(
        event_type=EventType.LEAVER,
        user_principal_name="leaver@contoso.onmicrosoft.com",
        correlation_id="corr-qp-5",
    )

    function_app.queue_processor(_queue_message(event))

    scheduled = wired.leaver_schedule_store.list_all()
    assert len(scheduled) == 1
    assert scheduled[0].user_principal_name == "leaver@contoso.onmicrosoft.com"


def test_events_poison_queue_records_failed_audit_for_parseable_message(wired):
    event = UserEvent(
        event_type=EventType.JOINER,
        user_principal_name="poisoned@contoso.onmicrosoft.com",
        correlation_id="corr-poison-1",
    )

    function_app.events_poison_queue(_queue_message(event))

    with open(wired.settings.local_audit_log_path, encoding="utf-8") as fh:
        lines = fh.readlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["action"] == "poison_queue"
    assert payload["correlation_id"] == "corr-poison-1"
    assert payload["result"] == "failed"


def test_events_poison_queue_handles_unparseable_message(wired):
    function_app.events_poison_queue(_queue_message(raw_body=b"garbage", msg_id="msg-99"))

    with open(wired.settings.local_audit_log_path, encoding="utf-8") as fh:
        lines = fh.readlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["action"] == "poison_queue"
    assert "msg-99" in payload["detail"]


def test_deferred_deletion_sweep_reports_due_leavers_without_deleting(wired):
    from datetime import date

    wired.leaver_schedule_store.schedule(
        "overdue@contoso.onmicrosoft.com", "corr-sweep-1", date(2020, 1, 1)
    )
    wired.leaver_schedule_store.schedule(
        "future@contoso.onmicrosoft.com", "corr-sweep-2", date(2999, 1, 1)
    )

    class _FakeTimerRequest:
        past_due = False

    function_app.deferred_deletion_sweep(_FakeTimerRequest())

    with open(wired.settings.local_audit_log_path, encoding="utf-8") as fh:
        lines = fh.readlines()
    payloads = [json.loads(line) for line in lines]
    reported_targets = {p["target"] for p in payloads if p["action"] == "deferred_deletion_due"}
    assert reported_targets == {"overdue@contoso.onmicrosoft.com"}

from __future__ import annotations

import json

from identity_lifecycle.models import EventType
from identity_lifecycle.parsing import parse_csv_bytes, parse_json_bytes


def test_parse_json_single_object_joiner():
    payload = json.dumps(
        {
            "event_type": "joiner",
            "user_principal_name": "new.hire@contoso.onmicrosoft.com",
            "display_name": "New Hire",
            "department": "Engineering",
            "personal_email": "new.hire@example.com",
        }
    ).encode()

    batch = parse_json_bytes(payload)

    assert batch.ok
    assert batch.event_count == 1
    event = batch.events[0]
    assert event.event_type == EventType.JOINER
    assert event.user_principal_name == "new.hire@contoso.onmicrosoft.com"
    assert event.department == "Engineering"


def test_parse_json_list_of_events():
    payload = json.dumps(
        [
            {
                "event_type": "mover",
                "user_principal_name": "a@contoso.onmicrosoft.com",
                "new_department": "Sales",
            },
            {
                "event_type": "leaver",
                "user_principal_name": "b@contoso.onmicrosoft.com",
                "last_day_of_work": "2026-08-01",
            },
        ]
    ).encode()

    batch = parse_json_bytes(payload)

    assert batch.ok
    assert batch.event_count == 2
    assert {e.event_type for e in batch.events} == {EventType.MOVER, EventType.LEAVER}


def test_parse_json_invalid_json_produces_issue_not_exception():
    batch = parse_json_bytes(b"{not valid json")
    assert not batch.ok
    assert batch.event_count == 0
    assert len(batch.issues) == 1


def test_parse_json_missing_required_field_is_a_row_level_issue():
    payload = json.dumps([{"event_type": "joiner"}]).encode()  # missing UPN

    batch = parse_json_bytes(payload)

    assert not batch.ok
    assert batch.event_count == 0
    assert "user_principal_name" in batch.issues[0].message


def test_parse_json_bad_row_does_not_abort_whole_batch():
    payload = json.dumps(
        [
            {"event_type": "joiner"},  # invalid: missing UPN
            {
                "event_type": "joiner",
                "user_principal_name": "good.user@contoso.onmicrosoft.com",
            },
        ]
    ).encode()

    batch = parse_json_bytes(payload)

    assert batch.event_count == 1
    assert len(batch.issues) == 1
    assert batch.events[0].user_principal_name == "good.user@contoso.onmicrosoft.com"


def test_parse_csv_bytes_basic():
    csv_text = (
        "event_type,user_principal_name,display_name,department,personal_email\n"
        "joiner,new.hire@contoso.onmicrosoft.com,New Hire,Engineering,new.hire@example.com\n"
        "joiner,other.hire@contoso.onmicrosoft.com,Other Hire,Sales,\n"
    )
    batch = parse_csv_bytes(csv_text.encode())

    assert batch.ok
    assert batch.event_count == 2
    assert batch.events[1].personal_email is None


def test_parse_csv_skips_blank_rows():
    csv_text = (
        "event_type,user_principal_name\n"
        "joiner,new.hire@contoso.onmicrosoft.com\n"
        ",\n"
        "joiner,second.hire@contoso.onmicrosoft.com\n"
    )
    batch = parse_csv_bytes(csv_text.encode())

    assert batch.event_count == 2


def test_parse_csv_unknown_column_goes_into_raw():
    csv_text = (
        "event_type,user_principal_name,cost_center\n"
        "joiner,new.hire@contoso.onmicrosoft.com,CC-100\n"
    )
    batch = parse_csv_bytes(csv_text.encode())

    assert batch.event_count == 1
    assert batch.events[0].raw.get("cost_center") == "CC-100"


def test_correlation_id_defaults_and_is_unique_per_event():
    csv_text = (
        "event_type,user_principal_name\n"
        "joiner,a@contoso.onmicrosoft.com\n"
        "joiner,b@contoso.onmicrosoft.com\n"
    )
    batch = parse_csv_bytes(csv_text.encode())
    ids = {e.correlation_id for e in batch.events}
    assert len(ids) == 2


def test_explicit_correlation_id_is_preserved():
    payload = json.dumps(
        {
            "event_type": "leaver",
            "user_principal_name": "leaver@contoso.onmicrosoft.com",
            "correlation_id": "fixed-corr-id-001",
        }
    ).encode()
    batch = parse_json_bytes(payload)
    assert batch.events[0].correlation_id == "fixed-corr-id-001"


def test_invalid_upn_without_at_sign_is_rejected():
    payload = json.dumps(
        {"event_type": "joiner", "user_principal_name": "not-an-upn"}
    ).encode()
    batch = parse_json_bytes(payload)
    assert not batch.ok


def test_invalid_event_type_is_rejected():
    payload = json.dumps(
        {"event_type": "promotion", "user_principal_name": "a@contoso.onmicrosoft.com"}
    ).encode()
    batch = parse_json_bytes(payload)
    assert not batch.ok

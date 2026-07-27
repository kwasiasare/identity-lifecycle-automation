"""Unit tests for the intake helper functions in function_app.py — the parts of
the trigger functions that don't require a live Azure Functions host to exercise
(the trigger bodies themselves are intentionally thin wrappers around these)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from function_app import build_intake_response, events_to_queue_messages, parse_intake_body

from identity_lifecycle.models import EventType, ParsedBatch, UserEvent


def test_parse_intake_body_json_by_default():
    body = json.dumps(
        {"event_type": "joiner", "user_principal_name": "a@contoso.onmicrosoft.com"}
    ).encode()
    batch = parse_intake_body(body, content_type="application/json")
    assert batch.ok
    assert batch.events[0].event_type == EventType.JOINER


def test_parse_intake_body_csv_by_content_type():
    body = b"event_type,user_principal_name\njoiner,a@contoso.onmicrosoft.com\n"
    batch = parse_intake_body(body, content_type="text/csv")
    assert batch.ok
    assert batch.event_count == 1


def test_parse_intake_body_csv_by_format_query_param():
    body = b"event_type,user_principal_name\njoiner,a@contoso.onmicrosoft.com\n"
    batch = parse_intake_body(body, content_type="text/plain", format_param="csv")
    assert batch.ok
    assert batch.event_count == 1


def test_events_to_queue_messages_serialises_each_event_as_json():
    event = UserEvent(event_type=EventType.JOINER, user_principal_name="a@contoso.onmicrosoft.com")
    batch = ParsedBatch(events=[event])
    messages = events_to_queue_messages(batch)
    assert len(messages) == 1
    decoded = json.loads(messages[0])
    assert decoded["user_principal_name"] == "a@contoso.onmicrosoft.com"


def test_events_to_queue_messages_empty_batch():
    assert events_to_queue_messages(ParsedBatch()) == []


def test_build_intake_response_all_valid_is_200():
    event = UserEvent(event_type=EventType.JOINER, user_principal_name="a@contoso.onmicrosoft.com")
    status, payload = build_intake_response(ParsedBatch(events=[event]))
    assert status == 200
    assert payload["accepted"] == 1
    assert payload["rejected"] == 0


def test_build_intake_response_partial_failure_is_207():
    from identity_lifecycle.models import ValidationIssue

    event = UserEvent(event_type=EventType.JOINER, user_principal_name="a@contoso.onmicrosoft.com")
    batch = ParsedBatch(events=[event], issues=[ValidationIssue(row_index=1, message="bad row")])
    status, payload = build_intake_response(batch)
    assert status == 207
    assert payload["accepted"] == 1
    assert payload["rejected"] == 1

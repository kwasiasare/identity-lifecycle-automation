from __future__ import annotations

import json

from identity_lifecycle.audit import AuditLogger


def test_record_appends_to_buffer(settings):
    audit = AuditLogger(settings)
    rec = audit.record(
        correlation_id="corr-1",
        event_type="joiner",
        action="create_user",
        target="a@contoso.onmicrosoft.com",
        result="success",
    )
    assert rec in audit.buffer
    assert len(audit.buffer) == 1


def test_local_fallback_used_when_log_analytics_not_configured(settings):
    assert settings.logs_ingestion_endpoint == ""
    audit = AuditLogger(settings)
    audit.record(
        correlation_id="corr-2",
        event_type="leaver",
        action="disable_account",
        target="b@contoso.onmicrosoft.com",
        result="success",
        detail="disabled",
    )
    with open(settings.local_audit_log_path, encoding="utf-8") as fh:
        lines = fh.readlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["correlation_id"] == "corr-2"
    assert payload["action"] == "disable_account"


def test_log_analytics_ingestion_success_does_not_fall_back(settings):
    settings_with_endpoint = settings.__class__(
        **{**settings.__dict__, "logs_ingestion_endpoint": "https://fake-dce.example.com",
           "logs_dcr_immutable_id": "dcr-fake-id"}
    )

    class _FakeIngestionClient:
        def __init__(self):
            self.uploaded = []

        def upload(self, *, rule_id, stream_name, logs):
            self.uploaded.append((rule_id, stream_name, logs))

    fake_client = _FakeIngestionClient()
    audit = AuditLogger(settings_with_endpoint, ingestion_client=fake_client)
    audit.record(
        correlation_id="corr-3",
        event_type="mover",
        action="update_attributes",
        target="c@contoso.onmicrosoft.com",
        result="success",
    )

    assert len(fake_client.uploaded) == 1
    rule_id, stream_name, logs = fake_client.uploaded[0]
    assert rule_id == "dcr-fake-id"
    assert logs[0]["correlation_id"] == "corr-3"

    # local fallback file must NOT have been written since ingestion succeeded
    import os

    assert not os.path.exists(settings_with_endpoint.local_audit_log_path)


def test_log_analytics_ingestion_failure_falls_back_to_local(settings):
    settings_with_endpoint = settings.__class__(
        **{**settings.__dict__, "logs_ingestion_endpoint": "https://fake-dce.example.com",
           "logs_dcr_immutable_id": "dcr-fake-id"}
    )

    class _BrokenIngestionClient:
        def upload(self, **kwargs):
            raise RuntimeError("simulated ingestion outage")

    audit = AuditLogger(settings_with_endpoint, ingestion_client=_BrokenIngestionClient())
    # must not raise despite the ingestion client raising
    audit.record(
        correlation_id="corr-4",
        event_type="joiner",
        action="create_user",
        target="d@contoso.onmicrosoft.com",
        result="success",
    )

    with open(settings_with_endpoint.local_audit_log_path, encoding="utf-8") as fh:
        lines = fh.readlines()
    assert len(lines) == 1
    assert "corr-4" in lines[0]

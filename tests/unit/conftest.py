from __future__ import annotations

import pytest

from identity_lifecycle.audit import AuditLogger
from identity_lifecycle.config import Settings, default_department_mappings
from tests.unit.fake_graph_client import FakeGraphClient


@pytest.fixture
def settings(tmp_path) -> Settings:
    """Default settings with the standard department mappings, no Log Analytics
    endpoint configured (forces the local-fallback audit path), and the local
    audit log redirected into pytest's tmp_path so tests never write into the repo."""
    return Settings(
        local_audit_log_path=str(tmp_path / "audit-fallback.log"),
        department_mappings=default_department_mappings(),
        welcome_mail_sender="no-reply@contoso.onmicrosoft.com",
    )


@pytest.fixture
def audit(settings: Settings) -> AuditLogger:
    return AuditLogger(settings)


@pytest.fixture
def fake_graph() -> FakeGraphClient:
    return FakeGraphClient()

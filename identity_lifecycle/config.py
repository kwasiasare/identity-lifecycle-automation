"""Runtime configuration loaded from environment variables (App Settings in Azure).

No secrets are read here beyond connection strings supplied by the platform
(managed identity is used for Graph and Log Analytics auth — see graph_client.py
and audit.py). Every setting has a safe local-dev default so the app can be
imported and unit tested without any Azure Functions host or cloud connectivity.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _env_bool(name: str, default: bool = False) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class DepartmentMapping:
    """Maps an HR department code to the Entra groups/license group a user needs."""

    department: str
    security_groups: tuple[str, ...] = field(default_factory=tuple)
    license_group: str | None = None


@dataclass(frozen=True)
class Settings:
    # Microsoft Graph
    graph_base_url: str = "https://graph.microsoft.com/v1.0"
    graph_beta_url: str = "https://graph.microsoft.com/beta"
    graph_scope: str = "https://graph.microsoft.com/.default"
    graph_request_timeout_seconds: float = 30.0
    # Bounded retry budget for 429/503 responses (honoring Retry-After) —
    # see GraphClient._request. Applied per HTTP call, not per flow.
    graph_max_retries: int = 4

    # Tenant / directory defaults
    default_domain: str = "contoso.onmicrosoft.com"
    default_usage_location: str = "GB"
    joiner_default_password_length: int = 16

    # Dedicated service/no-reply mailbox UPN used to send the joiner welcome
    # email. Must NOT be the just-created user — that mailbox isn't
    # provisioned yet and sendMail-as-self fails with
    # MailboxNotEnabledForRESTAPI. Empty string disables welcome mail (logged
    # as a failed step, never aborts the joiner flow).
    welcome_mail_sender: str = ""

    # Storage (queue intake + idempotency dedupe table)
    storage_connection_setting: str = "AzureWebJobsStorage"
    events_queue_name: str = "identity-events"
    inbound_container_name: str = "identity-events-inbound"
    idempotency_table_name: str = "IdempotencyLedger"
    # Durable leaver deferred-deletion schedule (PartitionKey="LeaverSchedule")
    # — see identity_lifecycle/leaver_schedule.py. Read by deferred_deletion_sweep.
    leaver_schedule_table_name: str = "LeaverSchedule"

    # Log Analytics custom table (Logs Ingestion API / DCR)
    logs_ingestion_endpoint: str = ""  # Data Collection Endpoint URL
    logs_dcr_immutable_id: str = ""  # Data Collection Rule immutable ID
    logs_stream_name: str = "Custom-IdentityLifecycleAudit_CL"
    local_audit_log_path: str = "audit-fallback.log"

    # Leaver policy
    leaver_deferred_delete_days: int = 30

    # Feature flags
    dry_run: bool = False

    department_mappings: dict[str, DepartmentMapping] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            graph_base_url=_env("GRAPH_BASE_URL", "https://graph.microsoft.com/v1.0"),
            graph_beta_url=_env("GRAPH_BETA_URL", "https://graph.microsoft.com/beta"),
            graph_max_retries=int(_env("GRAPH_MAX_RETRIES", "4")),
            default_domain=_env("DEFAULT_DOMAIN", "contoso.onmicrosoft.com"),
            default_usage_location=_env("DEFAULT_USAGE_LOCATION", "GB"),
            welcome_mail_sender=_env("WELCOME_MAIL_SENDER", ""),
            storage_connection_setting=_env(
                "STORAGE_CONNECTION_SETTING", "AzureWebJobsStorage"
            ),
            events_queue_name=_env("EVENTS_QUEUE_NAME", "identity-events"),
            inbound_container_name=_env(
                "INBOUND_CONTAINER_NAME", "identity-events-inbound"
            ),
            idempotency_table_name=_env("IDEMPOTENCY_TABLE_NAME", "IdempotencyLedger"),
            leaver_schedule_table_name=_env("LEAVER_SCHEDULE_TABLE_NAME", "LeaverSchedule"),
            logs_ingestion_endpoint=_env("LOGS_INGESTION_ENDPOINT"),
            logs_dcr_immutable_id=_env("LOGS_DCR_IMMUTABLE_ID"),
            logs_stream_name=_env(
                "LOGS_STREAM_NAME", "Custom-IdentityLifecycleAudit_CL"
            ),
            local_audit_log_path=_env("LOCAL_AUDIT_LOG_PATH", "audit-fallback.log"),
            leaver_deferred_delete_days=int(_env("LEAVER_DEFERRED_DELETE_DAYS", "30")),
            dry_run=_env_bool("DRY_RUN", False),
            department_mappings=default_department_mappings(),
        )


def default_department_mappings() -> dict[str, DepartmentMapping]:
    """Sensible placeholder mapping — override per tenant via a config blob/table later.

    Documented in README as a v1 simplification: department -> group mapping is
    static here; a production rollout would source this from an HR system of
    record or a Graph-backed configuration list.
    """
    defaults = [
        DepartmentMapping(
            department="Engineering",
            security_groups=("grp-engineering-all",),
            license_group="lic-m365-e5",
        ),
        DepartmentMapping(
            department="Sales",
            security_groups=("grp-sales-all",),
            license_group="lic-m365-e3",
        ),
        DepartmentMapping(
            department="Finance",
            security_groups=("grp-finance-all",),
            license_group="lic-m365-e3",
        ),
        DepartmentMapping(
            department="HR",
            security_groups=("grp-hr-all",),
            license_group="lic-m365-e3",
        ),
    ]
    return {m.department.lower(): m for m in defaults}


def get_settings() -> Settings:
    """Factory used by function bindings; re-reads env each call (cheap, testable)."""
    return Settings.from_env()


def all_managed_group_names(settings: Settings) -> set[str]:
    """Every group referenced by any department mapping — the set of groups this
    automation is allowed to add/remove membership for (mover reconciliation,
    leaver full removal). Groups outside this set are left untouched."""
    names: set[str] = set()
    for mapping in settings.department_mappings.values():
        names.update(mapping.security_groups)
        if mapping.license_group:
            names.add(mapping.license_group)
    return names

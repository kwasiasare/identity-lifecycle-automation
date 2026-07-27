"""Structured audit logging for every mutating (and skipped) action taken by a flow.

Primary sink: Azure Monitor Logs Ingestion API, writing to a custom Log
Analytics table (`Custom-IdentityLifecycleAudit_CL`) via a Data Collection Rule
(DCR) + Data Collection Endpoint (DCE) provisioned in infra/. Auth is via
DefaultAzureCredential (the function's managed identity needs the
"Monitoring Metrics Publisher"-equivalent DCR role — see infra/README notes).

Fallback sink: structured JSON lines to the Python logging module (which the
Functions host already ships to Application Insights / console). Used when
LOGS_INGESTION_ENDPOINT / LOGS_DCR_IMMUTABLE_ID aren't configured (local dev)
or when the ingestion call itself fails — audit logging must never be able to
take down a flow, so failures here are caught and logged, never raised.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from identity_lifecycle.config import Settings

logger = logging.getLogger("identity_lifecycle.audit")


@dataclass(frozen=True)
class AuditRecord:
    correlation_id: str
    event_type: str
    action: str
    target: str
    result: str  # "success" | "skipped" | "failed"
    detail: str = ""
    actor: str = "identity-lifecycle-automation"
    time_generated: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AuditLogger:
    """Call `.record(...)` for every decision a flow makes — including no-ops,
    since "checked, already in desired state, did nothing" is itself an
    auditable, idempotency-proving fact.
    """

    def __init__(self, settings: Settings, ingestion_client: Any | None = None) -> None:
        self._settings = settings
        self._ingestion_client = ingestion_client
        self._buffer: list[AuditRecord] = []  # exposed for tests / local inspection

    @property
    def buffer(self) -> list[AuditRecord]:
        return list(self._buffer)

    def record(
        self,
        *,
        correlation_id: str,
        event_type: str,
        action: str,
        target: str,
        result: str,
        detail: str = "",
        **extra: Any,
    ) -> AuditRecord:
        rec = AuditRecord(
            correlation_id=correlation_id,
            event_type=event_type,
            action=action,
            target=target,
            result=result,
            detail=detail,
            extra=extra,
        )
        self._buffer.append(rec)
        self._emit(rec)
        return rec

    def _emit(self, rec: AuditRecord) -> None:
        sent = False
        if self._settings.logs_ingestion_endpoint and self._settings.logs_dcr_immutable_id:
            sent = self._try_send_to_log_analytics(rec)
        if not sent:
            self._emit_local_fallback(rec)

    def _try_send_to_log_analytics(self, rec: AuditRecord) -> bool:
        try:
            client = self._ingestion_client or self._build_ingestion_client()
            # Log Analytics custom tables require the built-in `TimeGenerated`
            # column (exact casing) — map our snake_case field onto it without
            # losing it from the local/fallback JSON representation.
            log_entry = rec.to_dict()
            log_entry["TimeGenerated"] = log_entry["time_generated"]
            client.upload(
                rule_id=self._settings.logs_dcr_immutable_id,
                stream_name=self._settings.logs_stream_name,
                logs=[log_entry],
            )
            return True
        except Exception:  # noqa: BLE001 - audit sink must never raise into a flow
            logger.exception(
                "audit: Log Analytics ingestion failed, falling back to local log; "
                "correlation_id=%s action=%s",
                rec.correlation_id,
                rec.action,
            )
            return False

    def _build_ingestion_client(self):
        from azure.identity import DefaultAzureCredential
        from azure.monitor.ingestion import LogsIngestionClient

        credential = DefaultAzureCredential()
        client = LogsIngestionClient(
            endpoint=self._settings.logs_ingestion_endpoint, credential=credential
        )
        self._ingestion_client = client
        return client

    def _emit_local_fallback(self, rec: AuditRecord) -> None:
        line = json.dumps(rec.to_dict(), default=str)
        logger.info("AUDIT %s", line)
        try:
            with open(self._settings.local_audit_log_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            # Local file sink is best-effort only (e.g. read-only Functions
            # host filesystem in Azure) — the logging.info call above is the
            # durable fallback there (captured by App Insights).
            pass

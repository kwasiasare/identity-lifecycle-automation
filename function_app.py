"""Azure Functions v2 (Python) entrypoint.

Five triggers:
  - http_intake      : POST /api/events/intake — JSON or CSV body, one or many
                        events; enqueues one queue message per valid event.
  - blob_intake       : blob trigger on the inbound container — HR/IT ops drops
                         a CSV export there in place of a real HRIS webhook.
  - queue_processor    : queue trigger — dispatches each event to its flow.
  - events_poison_queue : queue trigger on the events queue's automatic
                         "-poison" queue — the host moves a message here once
                         it has exhausted host.json's maxDequeueCount. Records
                         a failed audit entry so a poisoned message is visible
                         in the audit trail, not just silently dropped.
  - deferred_deletion_sweep : daily timer — reports leaver accounts past their
                         deletion due date, read from the durable leaver
                         schedule ledger (identity_lifecycle/leaver_schedule.py).
                         Never deletes anything itself in v1 (see
                         identity_lifecycle/flows/leaver.py docstring); it
                         only audits what *would* be eligible, as a safety
                         rail against acting on a bad or replayed feed.

Queue/container names are bound via %AppSetting% indirection (not hardcoded
literals) so EVENTS_QUEUE_NAME / INBOUND_CONTAINER_NAME app settings actually
drive the bindings, matching identity_lifecycle/config.py's defaults.

All business logic lives in identity_lifecycle/ so it is unit-testable without
the Functions runtime — this file is intentionally thin: parse trigger input,
call into identity_lifecycle, log the outcome.
"""

from __future__ import annotations

import json
import logging
import os

import azure.functions as func

from identity_lifecycle.audit import AuditLogger
from identity_lifecycle.config import Settings, get_settings
from identity_lifecycle.flows.joiner import run_joiner
from identity_lifecycle.flows.leaver import run_leaver
from identity_lifecycle.flows.mover import run_mover
from identity_lifecycle.graph_client import GraphClient
from identity_lifecycle.idempotency import IdempotencyStore, InMemoryIdempotencyStore
from identity_lifecycle.leaver_schedule import InMemoryLeaverScheduleStore, LeaverScheduleStore
from identity_lifecycle.models import EventType, ParsedBatch, UserEvent
from identity_lifecycle.parsing import parse_csv_bytes, parse_json_bytes

logger = logging.getLogger("identity_lifecycle.function_app")

app = func.FunctionApp()

_FLOW_DISPATCH = {
    EventType.JOINER: run_joiner,
    EventType.MOVER: run_mover,
    EventType.LEAVER: run_leaver,
}

# Process-lifetime singletons so the in-memory fallbacks (used only when no
# storage connection string is configured, e.g. `func start` without Azurite)
# survive across invocations within the same worker process.
_fallback_idempotency_store = InMemoryIdempotencyStore()
_fallback_leaver_schedule_store = InMemoryLeaverScheduleStore()


def _build_idempotency_store(settings: Settings) -> IdempotencyStore:
    connection_string = os.environ.get(settings.storage_connection_setting)
    if not connection_string:
        return _fallback_idempotency_store
    from identity_lifecycle.idempotency import TableStorageIdempotencyStore

    return TableStorageIdempotencyStore(connection_string, settings.idempotency_table_name)


def _build_leaver_schedule_store(settings: Settings) -> LeaverScheduleStore:
    connection_string = os.environ.get(settings.storage_connection_setting)
    if not connection_string:
        return _fallback_leaver_schedule_store
    from identity_lifecycle.leaver_schedule import TableStorageLeaverScheduleStore

    return TableStorageLeaverScheduleStore(connection_string, settings.leaver_schedule_table_name)


# ---------------------------------------------------------------------------
# HTTP intake
# ---------------------------------------------------------------------------


@app.function_name(name="http_intake")
@app.route(route="events/intake", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
@app.queue_output(
    arg_name="outqueue",
    queue_name="%EVENTS_QUEUE_NAME%",
    connection="AzureWebJobsStorage",
)
def http_intake(req: func.HttpRequest, outqueue: func.Out[list[str]]) -> func.HttpResponse:
    content_type = req.headers.get("content-type") or ""
    format_param = req.params.get("format")
    body = req.get_body()

    batch = parse_intake_body(body, content_type=content_type, format_param=format_param)

    messages = events_to_queue_messages(batch)
    if messages:
        outqueue.set(messages)

    status, payload = build_intake_response(batch)
    return func.HttpResponse(
        json.dumps(payload), status_code=status, mimetype="application/json"
    )


def parse_intake_body(body: bytes, *, content_type: str = "", format_param: str | None = None) -> ParsedBatch:
    """Chooses CSV vs JSON parsing from the request's Content-Type header, with
    an explicit `?format=csv` query override for clients that can't set headers."""
    if "csv" in content_type.lower() or format_param == "csv":
        return parse_csv_bytes(body)
    return parse_json_bytes(body)


def build_intake_response(batch: ParsedBatch) -> tuple[int, dict]:
    """Status logic:
      - 400: nothing usable came out of the request at all — either zero rows
        parsed to anything (empty payload) or every parsed row was invalid.
        A client should not see 200/207 for "I accepted nothing".
      - 207: partial success — some events accepted, some rows rejected.
      - 200: every row accepted, nothing rejected.

    Response never echoes raw row content — `issues` carries row_index +
    field_names only (see ValidationIssue in models.py); the caller can
    correlate rejected rows back to their own submitted payload by position.
    """
    if batch.event_count == 0:
        status = 400
    elif batch.issues:
        status = 207  # 207 Multi-Status: partial success
    else:
        status = 200
    payload = {
        "accepted": batch.event_count,
        "rejected": len(batch.issues),
        "correlation_ids": [event.correlation_id for event in batch.events],
        "issues": [issue.model_dump(mode="json") for issue in batch.issues],
    }
    return status, payload


# ---------------------------------------------------------------------------
# Blob intake (CSV drop, standing in for an HRIS webhook)
# ---------------------------------------------------------------------------


@app.function_name(name="blob_intake")
@app.blob_trigger(
    arg_name="blob",
    path="%INBOUND_CONTAINER_NAME%/{name}",
    connection="AzureWebJobsStorage",
)
@app.queue_output(
    arg_name="outqueue",
    queue_name="%EVENTS_QUEUE_NAME%",
    connection="AzureWebJobsStorage",
)
def blob_intake(blob: func.InputStream, outqueue: func.Out[list[str]]) -> None:
    data = blob.read()
    batch = parse_csv_bytes(data, default_source=f"blob:{blob.name}")
    logger.info(
        "blob_intake: parsed %s (%d events, %d issues)",
        blob.name,
        batch.event_count,
        len(batch.issues),
    )
    for issue in batch.issues:
        # field_names only — never the row's actual values (may contain
        # arbitrary/sensitive HR columns).
        logger.warning(
            "blob_intake: row %d rejected: %s (fields: %s)",
            issue.row_index, issue.message, issue.field_names,
        )
    messages = events_to_queue_messages(batch)
    if messages:
        outqueue.set(messages)


def events_to_queue_messages(batch: ParsedBatch) -> list[str]:
    return [event.model_dump_json() for event in batch.events]


# ---------------------------------------------------------------------------
# Queue processor — the actual joiner/mover/leaver dispatch
# ---------------------------------------------------------------------------


@app.function_name(name="queue_processor")
@app.queue_trigger(
    arg_name="msg",
    queue_name="%EVENTS_QUEUE_NAME%",
    connection="AzureWebJobsStorage",
)
def queue_processor(msg: func.QueueMessage) -> None:
    settings = get_settings()
    audit = AuditLogger(settings)
    idempotency = _build_idempotency_store(settings)

    try:
        event = UserEvent.model_validate_json(msg.get_body())
    except Exception:
        logger.exception(
            "queue_processor: failed to parse queue message id=%s; dropping "
            "(dead-lettered by host after max retries — see events_poison_queue)",
            msg.id,
        )
        raise

    # The idempotency ledger is keyed on idempotency_key, not correlation_id
    # — correlation_id is purely a trace id and may be a fresh uuid4 every
    # parse of the same underlying row (see models.py).
    if idempotency.has_completed(event.event_type.value, event.idempotency_key):
        logger.info(
            "queue_processor: idempotency_key=%s (correlation_id=%s) already "
            "completed, skipping replay",
            event.idempotency_key, event.correlation_id,
        )
        audit.record(
            correlation_id=event.correlation_id,
            event_type=event.event_type.value,
            action="replay_short_circuit",
            target=event.user_principal_name,
            result="skipped",
            detail="event already marked completed in the idempotency ledger",
        )
        return

    flow = _FLOW_DISPATCH[event.event_type]
    with GraphClient(settings) as graph:
        try:
            if event.event_type == EventType.LEAVER:
                leaver_schedule = _build_leaver_schedule_store(settings)
                outcome = run_leaver(event, graph, audit, settings, leaver_schedule)
            else:
                outcome = flow(event, graph, audit, settings)
        except Exception as exc:  # noqa: BLE001 - must mark failed + surface to host retry
            idempotency.mark_failed(event.event_type.value, event.idempotency_key, detail=str(exc))
            logger.exception(
                "queue_processor: flow raised for correlation_id=%s (transient — "
                "message will retry/poison per host.json maxDequeueCount)",
                event.correlation_id,
            )
            raise

    if outcome.succeeded:
        idempotency.mark_completed(
            event.event_type.value, event.idempotency_key, detail=outcome.summary()
        )
    elif outcome.has_transient_failure:
        # A step looked transient (throttling/timeout/5xx that survived
        # GraphClient's own bounded retries) — don't mark this a permanent
        # failure. Raise so the host retries the message with a fresh
        # attempt/retry budget; if it exhausts maxDequeueCount it lands on
        # events_poison_queue below.
        idempotency.mark_failed(
            event.event_type.value, event.idempotency_key, detail=outcome.summary()
        )
        logger.error(
            "queue_processor: correlation_id=%s event_type=%s upn=%s -> transient "
            "failure, will retry: %s",
            event.correlation_id, event.event_type.value, event.user_principal_name,
            outcome.summary(),
        )
        audit.record(
            correlation_id=event.correlation_id,
            event_type=event.event_type.value,
            action="flow_outcome",
            target=event.user_principal_name,
            result="failed",
            detail=f"transient failure — message will retry: {outcome.summary()}",
        )
        raise RuntimeError(
            f"transient failure processing correlation_id={event.correlation_id}: {outcome.summary()}"
        )
    else:
        # A permanent (data/config) failure — e.g. manager UPN not found,
        # group missing. Retrying the identical message will fail the same
        # way every time, so record it and stop; do NOT raise (that would
        # just burn through maxDequeueCount for no benefit).
        idempotency.mark_failed(
            event.event_type.value, event.idempotency_key, detail=outcome.summary()
        )
        logger.error(
            "queue_processor: correlation_id=%s event_type=%s upn=%s -> permanent "
            "failure, not retrying: %s",
            event.correlation_id, event.event_type.value, event.user_principal_name,
            outcome.summary(),
        )
        audit.record(
            correlation_id=event.correlation_id,
            event_type=event.event_type.value,
            action="flow_outcome",
            target=event.user_principal_name,
            result="failed",
            detail=f"permanent failure — not retrying: {outcome.summary()}",
        )
        return

    logger.info(
        "queue_processor: correlation_id=%s event_type=%s upn=%s -> %s",
        event.correlation_id,
        event.event_type.value,
        event.user_principal_name,
        outcome.summary(),
    )


# ---------------------------------------------------------------------------
# Poison queue — messages that exhausted host.json's maxDequeueCount
# ---------------------------------------------------------------------------


@app.function_name(name="events_poison_queue")
@app.queue_trigger(
    arg_name="msg",
    queue_name="%EVENTS_QUEUE_NAME%-poison",
    connection="AzureWebJobsStorage",
)
def events_poison_queue(msg: func.QueueMessage) -> None:
    """The Storage Queue trigger extension automatically moves a message here
    once queue_processor has raised on it host.json's `maxDequeueCount` times
    (a transient failure that never recovered, or an unparseable message).
    This is the last stop — record a permanent failed audit entry so the
    poisoned event is visible in the audit trail instead of silently vanishing."""
    settings = get_settings()
    audit = AuditLogger(settings)
    body_text = msg.get_body().decode("utf-8", errors="replace")

    correlation_id, event_type, target = msg.id, "unknown", "unknown"
    try:
        event = UserEvent.model_validate_json(body_text)
        correlation_id = event.correlation_id
        event_type = event.event_type.value
        target = event.user_principal_name
    except Exception:
        logger.warning(
            "events_poison_queue: poisoned message id=%s is not a parseable UserEvent",
            msg.id,
        )

    logger.error(
        "events_poison_queue: message id=%s exhausted retries and was poisoned "
        "(correlation_id=%s)", msg.id, correlation_id,
    )
    audit.record(
        correlation_id=correlation_id,
        event_type=event_type,
        action="poison_queue",
        target=target,
        result="failed",
        detail=f"message dead-lettered after exhausting max dequeue attempts; queue message id={msg.id}",
    )


# ---------------------------------------------------------------------------
# Deferred deletion sweep — reports past-due leavers from the durable ledger
# ---------------------------------------------------------------------------


@app.function_name(name="deferred_deletion_sweep")
@app.timer_trigger(schedule="0 0 6 * * *", arg_name="timer", run_on_startup=False)
def deferred_deletion_sweep(timer: func.TimerRequest) -> None:
    """Runs daily. Reads the durable leaver schedule ledger
    (identity_lifecycle/leaver_schedule.py) and audits every account whose
    deletion due date has passed. v1 scope: this is intentionally
    reporting-only — it never calls Graph's delete endpoint. Wiring in the
    actual (confirmed, gated) delete call, requiring `settings.dry_run is
    False` and explicit confirmation, is tracked as a fast-follow once the
    flows above are validated end-to-end on the dev tenant.
    """
    from datetime import UTC, datetime

    settings = get_settings()
    audit = AuditLogger(settings)
    leaver_schedule = _build_leaver_schedule_store(settings)

    today = datetime.now(UTC).date()
    due_entries = leaver_schedule.list_due_on_or_before(today)

    logger.info(
        "deferred_deletion_sweep: %d leaver account(s) past their deferred-deletion "
        "due date as of %s — reporting only, no deletions performed",
        len(due_entries), today.isoformat(),
    )
    for entry in due_entries:
        audit.record(
            correlation_id=entry.correlation_id,
            event_type="leaver",
            action="deferred_deletion_due",
            target=entry.user_principal_name,
            result="info",
            detail=(
                f"deletion due date {entry.deletion_due_date.isoformat()} has passed; "
                "reporting only in v1 — no delete call made. See "
                "identity_lifecycle/flows/leaver.py for the deferred-deletion design."
            ),
            deletion_due_date=entry.deletion_due_date.isoformat(),
        )

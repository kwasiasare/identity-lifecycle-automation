"""Azure Functions v2 (Python) entrypoint.

Four triggers:
  - http_intake      : POST /api/events/intake — JSON or CSV body, one or many
                        events; enqueues one queue message per valid event.
  - blob_intake       : blob trigger on the inbound container — HR/IT ops drops
                         a CSV export there in place of a real HRIS webhook.
  - queue_processor    : queue trigger — dispatches each event to its flow.
  - deferred_deletion_sweep : daily timer — reports leaver accounts past their
                         deletion due date. Never deletes anything itself in v1
                         (see identity_lifecycle/flows/leaver.py docstring);
                         it only audits what *would* be eligible, as a safety
                         rail against acting on a bad or replayed feed.

All business logic lives in identity_lifecycle/ so it is unit-testable without
the Functions runtime — this file is intentionally thin: parse trigger input,
call into identity_lifecycle, log the outcome.
"""

from __future__ import annotations

import json
import logging

import azure.functions as func

from identity_lifecycle.audit import AuditLogger
from identity_lifecycle.config import get_settings
from identity_lifecycle.flows.joiner import run_joiner
from identity_lifecycle.flows.leaver import run_leaver
from identity_lifecycle.flows.mover import run_mover
from identity_lifecycle.graph_client import GraphClient
from identity_lifecycle.idempotency import IdempotencyStore, InMemoryIdempotencyStore
from identity_lifecycle.models import EventType, ParsedBatch, UserEvent
from identity_lifecycle.parsing import parse_csv_bytes, parse_json_bytes

logger = logging.getLogger("identity_lifecycle.function_app")

app = func.FunctionApp()

_FLOW_DISPATCH = {
    EventType.JOINER: run_joiner,
    EventType.MOVER: run_mover,
    EventType.LEAVER: run_leaver,
}

# Process-lifetime singleton so the in-memory idempotency fallback (used only
# when no storage connection string is configured, e.g. `func start` without
# Azurite) survives across invocations within the same worker process.
_fallback_idempotency_store = InMemoryIdempotencyStore()


def _build_idempotency_store(settings) -> IdempotencyStore:
    import os

    connection_string = os.environ.get(settings.storage_connection_setting)
    if not connection_string:
        return _fallback_idempotency_store
    from identity_lifecycle.idempotency import TableStorageIdempotencyStore

    return TableStorageIdempotencyStore(connection_string, settings.idempotency_table_name)


# ---------------------------------------------------------------------------
# HTTP intake
# ---------------------------------------------------------------------------


@app.function_name(name="http_intake")
@app.route(route="events/intake", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
@app.queue_output(
    arg_name="outqueue",
    queue_name="identity-events",
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
    status = 200 if batch.ok else 207  # 207 Multi-Status: partial success
    payload = {
        "accepted": batch.event_count,
        "rejected": len(batch.issues),
        "issues": [issue.model_dump(mode="json") for issue in batch.issues],
    }
    return status, payload


# ---------------------------------------------------------------------------
# Blob intake (CSV drop, standing in for an HRIS webhook)
# ---------------------------------------------------------------------------


@app.function_name(name="blob_intake")
@app.blob_trigger(
    arg_name="blob",
    path="identity-events-inbound/{name}",
    connection="AzureWebJobsStorage",
)
@app.queue_output(
    arg_name="outqueue",
    queue_name="identity-events",
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
        logger.warning("blob_intake: row %d rejected: %s", issue.row_index, issue.message)
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
    queue_name="identity-events",
    connection="AzureWebJobsStorage",
)
def queue_processor(msg: func.QueueMessage) -> None:
    settings = get_settings()
    audit = AuditLogger(settings)
    idempotency = _build_idempotency_store(settings)

    try:
        event = UserEvent.model_validate_json(msg.get_body())
    except Exception:
        logger.exception("queue_processor: failed to parse queue message; dropping (dead-lettered by host after max retries)")
        raise

    if idempotency.has_completed(event.event_type.value, event.correlation_id):
        logger.info(
            "queue_processor: correlation_id=%s already completed, skipping replay",
            event.correlation_id,
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
            outcome = flow(event, graph, audit, settings)
        except Exception as exc:  # noqa: BLE001 - must mark failed + surface to host retry
            idempotency.mark_failed(event.event_type.value, event.correlation_id, detail=str(exc))
            logger.exception(
                "queue_processor: flow failed for correlation_id=%s", event.correlation_id
            )
            raise

    if outcome.succeeded:
        idempotency.mark_completed(
            event.event_type.value, event.correlation_id, detail=outcome.summary()
        )
    else:
        idempotency.mark_failed(
            event.event_type.value, event.correlation_id, detail=outcome.summary()
        )
    logger.info(
        "queue_processor: correlation_id=%s event_type=%s upn=%s -> %s",
        event.correlation_id,
        event.event_type.value,
        event.user_principal_name,
        outcome.summary(),
    )


# ---------------------------------------------------------------------------
# Deferred deletion sweep — audit-only in v1, see leaver.py docstring
# ---------------------------------------------------------------------------


@app.function_name(name="deferred_deletion_sweep")
@app.timer_trigger(schedule="0 0 6 * * *", arg_name="timer", run_on_startup=False)
def deferred_deletion_sweep(timer: func.TimerRequest) -> None:
    """Runs daily. v1 scope: this is intentionally a reporting stub — it does
    not query or delete anything yet. Wiring it to the idempotency ledger's
    leaver records (to find accounts past their due date) and adding the
    actual Graph delete call, gated behind an explicit confirmation flag, is
    tracked as a fast-follow once the flows above are validated end-to-end on
    the dev tenant.
    """
    logger.info(
        "deferred_deletion_sweep: v1 stub — no deletions performed. "
        "See identity_lifecycle/flows/leaver.py for the deferred-deletion design."
    )

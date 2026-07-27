from __future__ import annotations

from identity_lifecycle.idempotency import InMemoryIdempotencyStore


def test_has_completed_false_when_never_seen():
    store = InMemoryIdempotencyStore()
    assert store.has_completed("joiner", "corr-1") is False


def test_mark_completed_then_has_completed_true():
    store = InMemoryIdempotencyStore()
    store.mark_completed("joiner", "corr-1", detail="ok")
    assert store.has_completed("joiner", "corr-1") is True


def test_mark_failed_does_not_count_as_completed():
    store = InMemoryIdempotencyStore()
    store.mark_failed("joiner", "corr-1", detail="boom")
    assert store.has_completed("joiner", "corr-1") is False
    entry = store.get("joiner", "corr-1")
    assert entry is not None
    assert entry.status == "failed"


def test_entries_are_scoped_by_event_type_and_correlation_id():
    store = InMemoryIdempotencyStore()
    store.mark_completed("joiner", "corr-1")
    assert store.has_completed("mover", "corr-1") is False
    assert store.has_completed("joiner", "corr-2") is False


def test_retry_after_failure_can_mark_completed():
    store = InMemoryIdempotencyStore()
    store.mark_failed("leaver", "corr-9", detail="transient error")
    assert store.has_completed("leaver", "corr-9") is False
    store.mark_completed("leaver", "corr-9", detail="succeeded on retry")
    assert store.has_completed("leaver", "corr-9") is True

"""Proves the sample payloads under samples/ (used in README/demo instructions)
actually conform to the current event schema — if a field is renamed, these
tests fail instead of the samples silently going stale."""

from __future__ import annotations

from pathlib import Path

from identity_lifecycle.models import EventType
from identity_lifecycle.parsing import parse_csv_bytes, parse_json_bytes

SAMPLES_DIR = Path(__file__).resolve().parents[2] / "samples"


def test_joiner_csv_sample_is_valid():
    batch = parse_csv_bytes((SAMPLES_DIR / "joiner.csv").read_bytes())
    assert batch.ok
    assert batch.event_count == 2
    assert all(e.event_type == EventType.JOINER for e in batch.events)


def test_joiner_json_sample_is_valid():
    batch = parse_json_bytes((SAMPLES_DIR / "joiner.json").read_bytes())
    assert batch.ok
    assert batch.events[0].event_type == EventType.JOINER
    assert batch.events[0].department == "Engineering"


def test_mover_csv_sample_is_valid():
    batch = parse_csv_bytes((SAMPLES_DIR / "mover.csv").read_bytes())
    assert batch.ok
    assert batch.events[0].event_type == EventType.MOVER
    assert batch.events[0].new_department == "Sales"


def test_mover_json_sample_is_valid():
    batch = parse_json_bytes((SAMPLES_DIR / "mover.json").read_bytes())
    assert batch.ok
    assert batch.events[0].new_job_title == "Solutions Engineer"


def test_leaver_csv_sample_is_valid():
    batch = parse_csv_bytes((SAMPLES_DIR / "leaver.csv").read_bytes())
    assert batch.ok
    assert batch.events[0].event_type == EventType.LEAVER


def test_leaver_json_sample_is_valid():
    batch = parse_json_bytes((SAMPLES_DIR / "leaver.json").read_bytes())
    assert batch.ok
    assert batch.events[0].last_day_of_work.isoformat() == "2026-10-15"

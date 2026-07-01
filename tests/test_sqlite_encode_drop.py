"""Ports SQLiteEncodeDropTests: an unencodable payload must be counted, not dropped silently."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest

from dprovenancekit import SQLiteTraceStore, TraceEvent, TracePriority, TraceableEvent


@dataclass(frozen=True)
class UnencodablePayload(TraceableEvent):
    tier_raw: int

    @property
    def type_identifier(self) -> str:
        return "unencodable"

    @property
    def priority(self) -> TracePriority:
        try:
            return TracePriority(self.tier_raw)
        except ValueError:
            return TracePriority.TELEMETRY

    def encode(self) -> bytes:
        raise ValueError("deliberately unencodable payload")


def _event(tier):
    return TraceEvent(
        run_id=uuid.uuid4(), context_id="ctx", engine_name="engine", schema_version=1,
        sequence=0, span_id=None, parent_span_id=None, payload=UnencodablePayload(int(tier)),
    )


def test_encode_failure_is_counted_not_silently_dropped(temp_db_path):
    store = SQLiteTraceStore(UnencodablePayload, temp_db_path)
    assert store.drop_stats.total == 0
    assert store.drop_stats.preserved_integrity

    store.record(_event(TracePriority.STRUCTURAL))

    assert store.drop_stats.structural == 1
    assert store.drop_stats.total == 1
    assert not store.drop_stats.preserved_integrity


def test_telemetry_encode_failure_is_counted_but_keeps_integrity(temp_db_path):
    store = SQLiteTraceStore(UnencodablePayload, temp_db_path)
    store.record(_event(TracePriority.TELEMETRY))
    store.record(_event(TracePriority.TELEMETRY))
    assert store.drop_stats.telemetry == 2
    assert store.drop_stats.preserved_integrity


def test_encode_drops_tally_per_tier(temp_db_path):
    store = SQLiteTraceStore(UnencodablePayload, temp_db_path)
    store.record(_event(TracePriority.TELEMETRY))
    store.record(_event(TracePriority.DIAGNOSTIC))
    store.record(_event(TracePriority.CRITICAL))
    stats = store.drop_stats
    assert stats.telemetry == 1
    assert stats.diagnostic == 1
    assert stats.critical == 1
    assert stats.total == 3
    assert not stats.preserved_integrity


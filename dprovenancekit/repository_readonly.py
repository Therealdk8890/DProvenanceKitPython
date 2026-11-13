"""Fail-closed, snapshot-consistent reader for sealing completed SQLite captures.

Never construct SQLiteTraceStore here: start_writer=False still creates a writable
connection, configures WAL, and executes schema initialization.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict

from .event import AnyTraceableEvent


class _SingleRunSnapshotStore:
    def __init__(self, run_id: str, snapshot: Dict[str, Any]) -> None:
        self.run_id = run_id
        self.snapshot = snapshot

    def repository_run_snapshot(self, run_id: uuid.UUID) -> Dict[str, Any]:
        if str(run_id) != self.run_id:
            raise ValueError("read-only snapshot run mismatch")
        return self.snapshot


def seal_readonly_run(repository: Any, path: str, run_id: Any) -> str:
    """Read one transactionally consistent, complete run without capture writes.

    SQLite's mode=ro blocks database mutation while preserving access to committed
    WAL frames. The source writer must have finished and persisted its quality
    counters before this call. A local administrator controlling the database
    remains outside the trust boundary.
    """
    key = str(uuid.UUID(str(run_id)))
    source = Path(path)
    if not source.is_file():
        raise ValueError("capture database does not exist")
    connection = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        meta = connection.execute(
            "SELECT context_id FROM runs WHERE run_id=?", (key,)
        ).fetchone()
        captured = connection.execute(
            "SELECT context_id, quality_json FROM run_capture WHERE run_id=?", (key,)
        ).fetchone()
        if meta is None or captured is None or not isinstance(meta[0], str):
            raise ValueError("run is missing durable capture metadata")
        context = meta[0]
        if captured[0] != context:
            raise ValueError("capture context mismatch")
        quality = json.loads(captured[1])
        if not isinstance(quality, dict):
            raise ValueError("invalid persisted capture quality")
        if (quality.get("lifecycle") != "completed"
                or quality.get("sealed") is not True
                or quality.get("schema_provenance") != "recorded"
                or quality.get("dropped_events") != 0
                or quality.get("dropped_edges") != 0
                or quality.get("undecoded_events") != 0
                or not isinstance(quality.get("workflow_id"), str)):
            raise ValueError("capture is incomplete or not prebound")

        rows = connection.execute(
            "SELECT id, context_id, engine, schema_version, sequence, span_id, "
            "parent_span_id, type, priority, payload, timestamp, schema_provenance "
            "FROM trace_events WHERE run_id=? ORDER BY sequence, id", (key,)
        ).fetchall()
        events = []
        sequences = set()
        for row in rows:
            (event_id, event_context, engine, schema_version, sequence,
             span_id, parent_span_id, event_type, priority, payload,
             timestamp_us, provenance) = row
            if (event_context != context or provenance != "recorded"
                    or type(sequence) is not int or sequence in sequences
                    or type(priority) is not int or not 0 <= priority <= 3
                    or type(schema_version) is not int or schema_version < 1
                    or not isinstance(event_type, str)
                    or not isinstance(payload, bytes)
                    or type(timestamp_us) is not int):
                raise ValueError("invalid capture event")
            uuid.UUID(event_id)
            AnyTraceableEvent.from_storage(payload, event_type, priority)
            sequences.add(sequence)
            events.append({
                "id": event_id, "run_id": key, "context_id": context,
                "engine_name": engine or "", "schema_version": schema_version,
                "sequence": sequence, "span_id": span_id,
                "parent_span_id": parent_span_id, "type_identifier": event_type,
                "priority": priority, "payload_bytes": payload,
                "timestamp_us": timestamp_us, "schema_provenance": provenance,
            })
        edge_rows = connection.execute(
            "SELECT source_id, target_id, edge_type FROM trace_edges WHERE "
            "source_id IN (SELECT id FROM trace_events WHERE run_id=?) OR "
            "target_id IN (SELECT id FROM trace_events WHERE run_id=?) "
            "ORDER BY source_id, target_id, edge_type", (key, key)
        ).fetchall()
        edges = [dict(zip(("source_id", "target_id", "type"), row))
                 for row in edge_rows]
        if (not events
                or type(quality.get("attempted_events")) is not int
                or type(quality.get("attempted_edges")) is not int
                or quality["attempted_events"] != len(events)
                or quality["attempted_edges"] != len(edges)):
            raise ValueError("capture counts do not match stored events and edges")
        snapshot = {
            "run": SimpleNamespace(context_id=context, capture_quality=quality),
            "events": events, "edges": edges, "capture_quality": quality,
            "lifecycle": quality["lifecycle"],
        }
    finally:
        connection.rollback()
        connection.close()
    return repository.seal_run(_SingleRunSnapshotStore(key, snapshot), key)

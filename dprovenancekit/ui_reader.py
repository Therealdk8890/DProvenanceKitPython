"""Non-mutating SQLite reader for the local trace viewer.

Unlike SQLiteTraceStore, this reader never initializes or reconciles a schema and
never starts or shuts down a writer. It opens existing databases with SQLite
URI mode=ro and issues SELECT-only queries.
"""

from __future__ import annotations

import sqlite3
import json
import uuid
from dataclasses import replace
from pathlib import Path
from typing import List, Optional, Type

from .event import AnyTraceableEvent, TraceEvent, TraceableEvent
from .query import CaptureQuality, TraceRun


class ReadOnlySQLiteTraceReader:
    """Read existing DProvenanceKit trace data without changing database bytes."""

    _REQUIRED_COLUMNS = {
        "runs": {"run_id", "context_id", "start_time", "end_time", "event_count", "fingerprint"},
        "trace_events": {
            "id", "run_id", "engine", "span_id", "parent_span_id",
            "type", "priority", "payload", "timestamp", "sequence",
        },
    }

    def __init__(
        self,
        event_type: Type[TraceableEvent],
        path: str,
    ) -> None:
        self.path = str(path)
        uri = Path(path).expanduser().resolve().as_uri() + "?mode=ro"
        self._db = sqlite3.connect(uri, uri=True)
        self._db.row_factory = None
        try:
            self._validate_schema()
        except BaseException:
            self._db.close()
            raise

        self._event_type = event_type
        self._event_columns = {row[1] for row in self._db.execute("PRAGMA table_info(trace_events)")}
        self._has_capture = bool(self._db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='run_capture'"
        ).fetchone())

    def _validate_schema(self) -> None:
        tables = {
            row[0]
            for row in self._db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        for table, required in self._REQUIRED_COLUMNS.items():
            if table not in tables:
                raise sqlite3.DatabaseError(f"missing required table: {table}")
            columns = {
                row[1] for row in self._db.execute(f"PRAGMA table_info({table})")
            }
            missing = sorted(required - columns)
            if missing:
                raise sqlite3.DatabaseError(
                    f"table {table} is missing required columns: {', '.join(missing)}"
                )

    def list_run_metadata(self) -> List[dict]:
        rows = self._db.execute(
            "SELECT run_id, context_id, start_time, end_time, event_count, fingerprint "
            "FROM runs ORDER BY start_time DESC, run_id DESC"
        ).fetchall()
        return [
            {
                "run_id": row[0],
                "context_id": row[1],
                "start_time": int(row[2] or 0),
                "end_time": int(row[3] or 0),
                "event_count": int(row[4] or 0),
                "fingerprint": row[5] or "",
            }
            for row in rows
            if row[0] is not None
        ]

    def get_run(self, run_id: uuid.UUID) -> Optional[TraceRun]:
        rows = self._db.execute(
            "SELECT context_id FROM runs WHERE run_id = ?",
            (str(run_id),),
        ).fetchall()
        if not rows or rows[0][0] is None:
            return None
        context_id = rows[0][0]
        sql = (
            "SELECT id, engine, span_id, parent_span_id, payload, timestamp, sequence, type, priority, "
            "schema_version, schema_provenance FROM trace_events WHERE run_id=? ORDER BY sequence"
            if {"schema_version", "schema_provenance"} <= self._event_columns else
            "SELECT id, engine, span_id, parent_span_id, payload, timestamp, sequence, type, priority, "
            "1, 'legacy_assumed_v1' FROM trace_events WHERE run_id=? ORDER BY sequence"
        )
        event_rows = self._db.execute(
            sql,
            (str(run_id),),
        ).fetchall()

        events = []
        undecoded = 0
        assumed_schema = False
        for (event_id, engine, span_id, parent_span_id, payload_data, timestamp_us,
             sequence, event_type, priority, schema_version, provenance) in event_rows:
            assumed_schema = assumed_schema or provenance != "recorded"
            try:
                payload = (
                    AnyTraceableEvent.from_storage(bytes(payload_data), event_type, priority)
                    if self._event_type is AnyTraceableEvent
                    else self._event_type.decode(bytes(payload_data))
                )
                parsed_event_id = uuid.UUID(event_id)
            except Exception:
                undecoded += 1
                continue
            events.append(
                TraceEvent(
                    id=parsed_event_id,
                    run_id=run_id,
                    context_id=context_id,
                    engine_name=engine if engine is not None else "Unknown",
                    schema_version=int(schema_version),
                    sequence=int(sequence),
                    span_id=span_id,
                    parent_span_id=parent_span_id,
                    payload=payload,
                    timestamp=float(timestamp_us) / 1_000_000.0,
                )
            )
        quality = CaptureQuality()
        if self._has_capture:
            row = self._db.execute("SELECT quality_json FROM run_capture WHERE run_id=?", (str(run_id),)).fetchone()
            if row:
                quality = CaptureQuality(**json.loads(row[0]))
        quality = replace(
            quality, undecoded_events=quality.undecoded_events + undecoded,
            schema_provenance="legacy_assumed_v1" if assumed_schema else quality.schema_provenance,
        )
        return TraceRun(run_id=run_id, context_id=context_id, events=events, capture_quality=quality)

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "ReadOnlySQLiteTraceReader":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

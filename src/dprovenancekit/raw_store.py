"""The viewer's read path: reopen a database written by another connection."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import List, Optional

from .sqlite_store import SQLiteConnection


@dataclass(frozen=True)
class RawTraceEvent:
    run_id: uuid.UUID
    context_id: str
    priority: int
    sequence: int
    engine_name: str
    span_id: Optional[str]
    parent_span_id: Optional[str]
    type_identifier: str
    payload_json: str
    timestamp: float
    id: uuid.UUID = field(default_factory=uuid.uuid4)


@dataclass(frozen=True)
class RawTraceRun:
    run_id: uuid.UUID
    context_id: str
    start_time: float
    end_time: float
    event_count: int
    events: List[RawTraceEvent]


class RawTraceStore:
    """Reads runs and their events without the generic event type — payloads stay JSON."""

    def __init__(self, path: str):
        self._db = SQLiteConnection(path)

    def fetch_all_runs(self) -> List[RawTraceRun]:
        rows = self._db.query(
            "SELECT run_id, context_id, start_time, end_time, event_count "
            "FROM runs ORDER BY start_time DESC"
        )
        runs: List[RawTraceRun] = []
        for run_id_str, context_id, start_ms, end_ms, event_count in rows:
            try:
                run_id = uuid.UUID(run_id_str)
            except (ValueError, AttributeError):
                continue
            if context_id is None:
                continue
            events = self._fetch_events_for_run(run_id_str)
            runs.append(
                RawTraceRun(
                    run_id=run_id,
                    context_id=context_id,
                    start_time=float(start_ms or 0) / 1_000_000.0,
                    end_time=float(end_ms or 0) / 1_000_000.0,
                    event_count=int(event_count or 0),
                    events=events,
                )
            )
        return runs

    def _fetch_events_for_run(self, run_id_str: str) -> List[RawTraceEvent]:
        rows = self._db.query(
            "SELECT context_id, priority, sequence, engine, span_id, parent_span_id, "
            "type, payload, timestamp FROM trace_events WHERE run_id = ? ORDER BY sequence ASC",
            (run_id_str,),
        )
        try:
            run_id = uuid.UUID(run_id_str)
        except (ValueError, AttributeError):
            run_id = uuid.uuid4()

        events: List[RawTraceEvent] = []
        for (
            context_id,
            priority,
            sequence,
            engine,
            span_id,
            parent_span_id,
            type_,
            payload,
            ts,
        ) in rows:
            try:
                payload_json = bytes(payload).decode("utf-8")
            except Exception:
                payload_json = "{}"
            events.append(
                RawTraceEvent(
                    run_id=run_id,
                    context_id=context_id or "",
                    priority=int(priority),
                    sequence=int(sequence),
                    engine_name=engine or "Unknown",
                    span_id=span_id,
                    parent_span_id=parent_span_id,
                    type_identifier=type_ or "Unknown",
                    payload_json=payload_json,
                    timestamp=float(ts or 0) / 1_000_000.0,
                )
            )
        return events

"""Ports SQLiteInsertFailureDropTests: a failed batch insert is tallied, not silent."""

from __future__ import annotations

import uuid

from dprovenancekit import (
    SQLiteConnection,
    SQLiteWriter,
    TracePriority,
    TraceDropTally,
    TraceWriteBuffer,
)
from dprovenancekit.event import TraceEventRow


def _row(tier, seq, run="run-1"):
    return TraceEventRow(
        id=str(uuid.uuid4()),
        run_id=run,
        context_id="ctx",
        priority=int(tier),
        sequence=seq,
        engine="engine",
        span_id=None,
        parent_span_id=None,
        type="event",
        payload=b"{}",
        timestamp=1_000 + seq,
    )


def _create_runs_table(db):
    db.execute(
        "CREATE TABLE runs (run_id TEXT PRIMARY KEY, context_id TEXT, start_time INTEGER, "
        "end_time INTEGER, event_count INTEGER, fingerprint TEXT);"
    )


def _create_trace_events_table(db):
    db.execute(
        "CREATE TABLE trace_events (id TEXT PRIMARY KEY, run_id TEXT NOT NULL, context_id TEXT NOT NULL, "
        "priority INTEGER NOT NULL, sequence INTEGER NOT NULL, engine TEXT, span_id TEXT, "
        "parent_span_id TEXT, type TEXT NOT NULL, payload BLOB NOT NULL, timestamp INTEGER NOT NULL);"
    )


def _count(db, sql):
    rows = db.query(sql)
    return rows[0][0] if rows else 0


def test_failed_batch_insert_is_tallied_and_breaks_integrity(temp_db_path):
    db = SQLiteConnection(temp_db_path)
    _create_runs_table(
        db
    )  # omit trace_events so the batch INSERT is guaranteed to fail

    buffer = TraceWriteBuffer(max_global_buffer=1_000)
    tally = TraceDropTally()
    writer = SQLiteWriter(db, buffer, tally)

    buffer.enqueue(_row(TracePriority.STRUCTURAL, 0))
    buffer.enqueue(_row(TracePriority.TELEMETRY, 1))

    writer.flush()

    stats = tally.snapshot
    assert stats.structural == 1
    assert stats.telemetry == 1
    assert stats.total == 2
    assert not stats.preserved_integrity

    assert _count(db, "SELECT COUNT(*) FROM runs;") == 0


def test_successful_insert_tallies_nothing_and_records_accurate_metadata(temp_db_path):
    db = SQLiteConnection(temp_db_path)
    _create_runs_table(db)
    _create_trace_events_table(db)

    buffer = TraceWriteBuffer(max_global_buffer=1_000)
    tally = TraceDropTally()
    writer = SQLiteWriter(db, buffer, tally)

    buffer.enqueue(_row(TracePriority.STRUCTURAL, 0))
    buffer.enqueue(_row(TracePriority.CRITICAL, 1))
    buffer.enqueue(_row(TracePriority.TELEMETRY, 2))

    writer.flush()

    assert tally.snapshot.total == 0
    assert tally.snapshot.preserved_integrity

    assert _count(db, "SELECT COUNT(*) FROM trace_events;") == 3
    assert _count(db, "SELECT event_count FROM runs WHERE run_id = 'run-1';") == 3

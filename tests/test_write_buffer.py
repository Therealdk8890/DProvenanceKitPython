"""Ports TraceWriteBufferTests."""

from __future__ import annotations

from dprovenancekit import TracePriority, TraceWriteBuffer
from dprovenancekit.event import TraceEventRow


def _make_row(run_id, seq, priority):
    return TraceEventRow(
        id=f"{run_id}-{seq}",
        run_id=run_id,
        context_id=run_id,
        priority=int(priority),
        sequence=seq,
        engine="E",
        span_id=None,
        parent_span_id=None,
        type="critical" if priority == TracePriority.CRITICAL else "telemetry",
        payload=b"",
        timestamp=seq,
    )


def test_drain_preserves_global_insertion_order():
    buffer = TraceWriteBuffer(max_global_buffer=10_000, max_per_run_buffer=10_000)
    priorities = [
        TracePriority.TELEMETRY,
        TracePriority.CRITICAL,
        TracePriority.STRUCTURAL,
        TracePriority.DIAGNOSTIC,
    ]
    for i in range(200):
        buffer.enqueue(_make_row("r", i, priorities[i % len(priorities)]))

    drained = buffer.flush_all()
    assert len(drained) == 200
    assert [d.sequence for d in drained] == list(range(200))
    assert buffer.current_depth == 0


def test_heavy_burst_sheds_telemetry_but_keeps_critical():
    import sys

    cap = 20_000
    buffer = TraceWriteBuffer(max_global_buffer=cap, max_per_run_buffer=sys.maxsize)

    total = 200_000
    critical_every = 1_000
    criticals_enqueued = 0
    for i in range(total):
        is_critical = i % critical_every == 0
        if is_critical:
            criticals_enqueued += 1
        buffer.enqueue(
            _make_row("rogue", i, TracePriority.CRITICAL if is_critical else TracePriority.TELEMETRY)
        )

    assert buffer.current_depth <= cap
    drained = buffer.flush_all()
    assert len(drained) <= cap

    surviving_criticals = sum(1 for d in drained if d.type == "critical")
    assert surviving_criticals == criticals_enqueued


def test_per_run_soft_cap_keeps_critical_events():
    buffer = TraceWriteBuffer(max_global_buffer=100_000, max_per_run_buffer=50)

    buffer.enqueue(_make_row("run", 0, TracePriority.CRITICAL))
    for i in range(1, 501):
        buffer.enqueue(_make_row("run", i, TracePriority.TELEMETRY))
    buffer.enqueue(_make_row("run", 501, TracePriority.CRITICAL))

    drops = buffer.drop_stats
    drained = buffer.flush_all()
    criticals = sum(1 for d in drained if d.type == "critical")
    assert criticals == 2
    assert len(drained) < 502

    assert len(drained) + drops.total == 502
    assert drops.telemetry == drops.total
    assert drops.preserved_integrity


def test_global_eviction_is_counted():
    import sys

    cap = 100
    buffer = TraceWriteBuffer(max_global_buffer=cap, max_per_run_buffer=sys.maxsize)

    for i in range(cap):
        buffer.enqueue(_make_row("r", i, TracePriority.TELEMETRY))
    critical_count = 10
    for i in range(critical_count):
        buffer.enqueue(_make_row("r", cap + i, TracePriority.CRITICAL))

    drops = buffer.drop_stats
    assert drops.telemetry == critical_count
    assert drops.preserved_integrity

    drained = buffer.flush_all()
    assert len(drained) + drops.total == cap + critical_count
    assert sum(1 for d in drained if d.type == "critical") == critical_count

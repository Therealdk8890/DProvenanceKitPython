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


def test_per_run_depth_counter_does_not_drift_after_global_eviction():
    """The per-run depth counter must track actual occupancy. When global-capacity eviction
    pops a victim from the enqueuing run, writing back a pre-eviction ``run_depth + 1``
    left the counter permanently inflated, spuriously tripping the soft per-run cap and
    shedding a run's events while its real occupancy was far below the limit."""
    buffer = TraceWriteBuffer(max_global_buffer=10, max_per_run_buffer=40)
    for i in range(60):  # far past the global cap, one run
        buffer.enqueue(_make_row("r", i, TracePriority.TELEMETRY))

    # Only 10 rows can be buffered; the counter must agree, not read 40.
    assert buffer.current_depth == 10
    assert buffer._queue_depth_by_run.get("r") == 10

    buffer.flush_all()
    # Emptied buffer -> no residual phantom count for the run.
    assert buffer.current_depth == 0
    assert not buffer._queue_depth_by_run.get("r")


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
            _make_row(
                "rogue",
                i,
                TracePriority.CRITICAL if is_critical else TracePriority.TELEMETRY,
            )
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


def test_requeue_is_bounded_and_counts_shed_rows():
    """A persistently failing writer keeps draining the tiers into the retry queue via
    requeue(); without a bound that queue would grow without limit. It must stay capped
    at the configured global capacity, shedding the oldest retried rows and counting
    them as drops."""
    import sys

    from dprovenancekit.config import BufferCapacity, EvictionPolicy, OfflineConfig

    cap = 10
    config = OfflineConfig(
        capacity=BufferCapacity(
            max_items=cap, max_bytes=sys.maxsize, max_event_size_bytes=sys.maxsize
        ),
        eviction=EvictionPolicy.DROP_OLDEST,
    )
    buffer = TraceWriteBuffer(config=config)

    # Simulate the writer draining and re-queuing failed batches repeatedly, as it would
    # against a locked database. Each round pushes a fresh failed row to the front.
    total_requeued = 0
    for seq in range(100):
        buffer.requeue([_make_row("run", seq, TracePriority.STRUCTURAL)], [])
        total_requeued += 1

    # The retry backlog surfaces in current_depth (so the load signal isn't blind to it)
    # and is bounded by the configured capacity.
    assert buffer.current_depth <= cap
    # Everything shed from the retry queue is accounted for as a drop.
    drained = buffer.flush_all()
    assert len(drained) + buffer.drop_stats.total == total_requeued
    # The freshest rows survive: requeue prepends, so the last row enqueued is drained first.
    assert drained[0].sequence == 99


def test_requeue_unbounded_config_keeps_all_retries():
    """When the buffer is explicitly configured unbounded, the retry backlog is unbounded
    by design and nothing is shed."""
    import sys

    from dprovenancekit.config import BufferCapacity, EvictionPolicy, OfflineConfig

    config = OfflineConfig(
        capacity=BufferCapacity(
            max_items=sys.maxsize,
            max_bytes=sys.maxsize,
            max_event_size_bytes=sys.maxsize,
        ),
        eviction=EvictionPolicy.DROP_OLDEST,
    )
    buffer = TraceWriteBuffer(config=config)
    for seq in range(2000):
        buffer.requeue([_make_row("run", seq, TracePriority.STRUCTURAL)], [])
    assert buffer.drop_stats.total == 0
    assert len(buffer.flush_all()) == 2000

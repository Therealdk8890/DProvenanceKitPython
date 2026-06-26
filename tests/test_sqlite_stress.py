"""Ports SQLiteStressTests."""

from __future__ import annotations

import threading

from dprovenancekit import DProvenanceKit, SQLiteTraceStore, TraceQueryDSL
from conftest import TestEvent


def test_concurrency_10k(temp_db_path):
    store = SQLiteTraceStore(TestEvent, temp_db_path, max_global_buffer=10_000, max_per_run_buffer=1000)
    kit = DProvenanceKit(TestEvent)

    def worker(i):
        with kit.run(context_id=f"stress_test_{i}", store=store):
            kit.record(TestEvent.process_started())
            for j in range(98):
                kit.record(TestEvent.step_completed(j))
            kit.record(TestEvent.process_finished())

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(100)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    store.flush()

    runs = store.query_runs(TraceQueryDSL().requiring_step("processFinished"))
    assert len(runs) == 100
    total_events = sum(len(r.events) for r in runs)
    assert total_events == 10000


def test_query_engine(temp_db_path):
    store = SQLiteTraceStore(TestEvent, temp_db_path, max_global_buffer=10_000, max_per_run_buffer=1000)
    kit = DProvenanceKit(TestEvent)

    with kit.run(context_id="q1", store=store):
        kit.record(TestEvent.process_started())
        kit.record(TestEvent.step_completed(1))
        kit.record(TestEvent.error_detected())
        kit.record(TestEvent.process_finished())

    with kit.run(context_id="q2", store=store):
        kit.record(TestEvent.process_started())
        kit.record(TestEvent.step_completed(1))
        kit.record(TestEvent.process_finished())

    store.flush()

    runs = store.query_runs(
        TraceQueryDSL().requiring_step("processFinished").requiring_step("errorDetected")
    )
    assert len(runs) == 1
    assert runs[0].context_id == "q1"

    seq_runs = store.query_runs(
        TraceQueryDSL().requiring_sequence(["processStarted", "errorDetected", "processFinished"])
    )
    assert len(seq_runs) == 1


def test_flush_is_barrier_and_preserves_record_order(temp_db_path):
    store = SQLiteTraceStore(TestEvent, temp_db_path, max_global_buffer=10_000, max_per_run_buffer=1000)
    kit = DProvenanceKit(TestEvent)
    n = 500

    with kit.run(context_id="barrier", store=store):
        kit.record(TestEvent.process_started())
        for j in range(n - 2):
            kit.record(TestEvent.step_completed(j))
        kit.record(TestEvent.process_finished())

    store.flush()

    runs = store.query_runs(TraceQueryDSL().filter_context_id("barrier"))
    assert len(runs) == 1
    events = runs[0].events
    assert len(events) == n
    assert [e.sequence for e in events] == list(range(n))


def test_burst_ingestion_collapse(tmp_path):
    burst_path = str(tmp_path / "burst.sqlite")
    store = SQLiteTraceStore(TestEvent, burst_path, max_global_buffer=100, max_per_run_buffer=50)
    kit = DProvenanceKit(TestEvent)

    with kit.run(context_id="rogue_agent", store=store):
        kit.record(TestEvent.process_started())  # critical, should survive
        for j in range(200):
            kit.record(TestEvent.step_completed(j))  # telemetry, should drop
        kit.record(TestEvent.process_finished())  # critical, should survive

    store.flush()

    runs = store.query_runs(TraceQueryDSL().requiring_step("processStarted"))
    assert len(runs) == 1
    events = runs[0].events

    has_start = any(e.payload.type_identifier == "processStarted" for e in events)
    has_end = any(e.payload.type_identifier == "processFinished" for e in events)
    assert has_start
    assert has_end
    assert len(events) < 202

    drops = store.drop_stats
    assert drops.total > 0
    assert drops.telemetry > 0
    assert drops.critical == 0
    assert drops.structural == 0
    assert drops.preserved_integrity

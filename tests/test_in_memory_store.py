"""Ports InMemoryTraceStoreTests."""

from __future__ import annotations

import threading
import time

from dprovenancekit import (
    DProvenanceKit,
    InMemoryTraceStore,
    LiveTraceQueryEngine,
    TraceQueryDSL,
    TraceQuerySubscription,
)
from conftest import TestEvent


def test_record_is_immediately_queryable_in_order():
    store = InMemoryTraceStore()
    kit = DProvenanceKit(TestEvent)
    n = 500

    with kit.run(context_id="mem", store=store):
        kit.record(TestEvent.process_started())
        for j in range(n - 2):
            kit.record(TestEvent.step_completed(j))
        kit.record(TestEvent.process_finished())

    runs = store.query_runs(TraceQueryDSL().filter_context_id("mem"))
    assert len(runs) == 1

    events = runs[0].events
    assert len(events) == n
    assert [e.sequence for e in events] == list(range(n))


def test_concurrent_runs_remain_consistent():
    store = InMemoryTraceStore()
    kit = DProvenanceKit(TestEvent)

    def worker(i):
        with kit.run(context_id=f"run_{i}", store=store):
            kit.record(TestEvent.process_started())
            for j in range(8):
                kit.record(TestEvent.step_completed(j))
            kit.record(TestEvent.process_finished())

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    runs = store.query_runs(TraceQueryDSL().requiring_step("processFinished"))
    assert len(runs) == 50
    total = sum(len(r.events) for r in runs)
    assert total == 50 * 10
    for run in runs:
        assert [e.sequence for e in run.events] == list(range(10))


class _CapturingSubscription(TraceQuerySubscription):
    def __init__(self, query):
        import uuid

        self.query_id = uuid.uuid4()
        self.query = query
        self._lock = threading.Lock()
        self.ids = []

    def on_match(self, run):
        with self._lock:
            self.ids.append(run.run_id)

    def on_update(self, run):
        pass

    @property
    def count(self):
        with self._lock:
            return len(self.ids)


def test_live_engine_receives_ordered_delivery():
    engine = LiveTraceQueryEngine()
    store = InMemoryTraceStore(live_engine=engine)
    sub = _CapturingSubscription(TraceQueryDSL().requiring_step("processFinished"))
    engine.register(sub)

    kit = DProvenanceKit(TestEvent)
    with kit.run(context_id="live", store=store):
        kit.record(TestEvent.process_started())
        kit.record(TestEvent.process_finished())

    deadline = time.time() + 2.0
    while time.time() < deadline and sub.count != 1:
        time.sleep(0.005)
    assert sub.count == 1


class _FlakySubscription(TraceQuerySubscription):
    """Raises on its first delivery, records subsequent ones."""

    def __init__(self, query):
        import uuid

        self.query_id = uuid.uuid4()
        self.query = query
        self._lock = threading.Lock()
        self.ids = []
        self._raised = False

    def on_match(self, run):
        with self._lock:
            if not self._raised:
                self._raised = True
                raise RuntimeError("subscriber boom")
            self.ids.append(run.run_id)

    def on_update(self, run):
        pass

    @property
    def count(self):
        with self._lock:
            return len(self.ids)


def test_live_consumer_survives_a_raising_subscriber():
    """A subscriber callback raising must not kill the shared live-consumer daemon thread
    and silently stop *all* future delivery. After the first delivery raises, a later
    matching run must still be delivered."""
    engine = LiveTraceQueryEngine()
    store = InMemoryTraceStore(live_engine=engine)
    sub = _FlakySubscription(TraceQueryDSL().requiring_step("processFinished"))
    engine.register(sub)

    kit = DProvenanceKit(TestEvent)
    # First matching run: its on_match raises (would kill an unguarded consumer thread).
    with kit.run(context_id="live-1", store=store):
        kit.record(TestEvent.process_started())
        kit.record(TestEvent.process_finished())
    # Second matching run: must still be delivered because the thread survived.
    with kit.run(context_id="live-2", store=store):
        kit.record(TestEvent.process_started())
        kit.record(TestEvent.process_finished())

    deadline = time.time() + 2.0
    while time.time() < deadline and sub.count != 1:
        time.sleep(0.005)
    assert sub.count == 1

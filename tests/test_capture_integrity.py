"""Evidence admission must not turn dropped or misdecoded critical steps green."""

from dataclasses import dataclass
import json
import threading
import uuid

import pytest

from dprovenancekit import (
    AnyTraceableEvent, DProvenanceKit, InMemoryTraceStore, RegressionGate,
    SQLiteTraceStore, TraceEvent, TracePriority, TraceableEvent,
)
from dprovenancekit.edge import TraceEdgeType
from dprovenancekit.ui_reader import ReadOnlySQLiteTraceReader


@dataclass(frozen=True)
class SafetyCheck(TraceableEvent):
    outcome: str

    @property
    def type_identifier(self):
        return "safety.check"

    @property
    def priority(self):
        return TracePriority.CRITICAL


def record_check(store, outcome="verified", schema=7):
    kit = DProvenanceKit(SafetyCheck)
    with kit.run("case", store, schema_version=schema) as active:
        kit.record(SafetyCheck(outcome))
    return active


def test_custom_critical_identity_schema_and_changed_payload_survive_generic_reload(tmp_path):
    path = str(tmp_path / "capture.sqlite")
    with SQLiteTraceStore(AnyTraceableEvent, path, start_writer=False) as store:
        first = record_check(store)
        second = record_check(store, "bypassed")
        golden, candidate = store.get_run(first.run_id), store.get_run(second.run_id)
        assert golden.events[0].payload.type_identifier == "safety.check"
        assert golden.events[0].payload.priority == TracePriority.CRITICAL
        assert golden.events[0].schema_version == 7
        assert not RegressionGate().check(golden, candidate).passed
        selected = store.get_events({golden.events[0].id})[golden.events[0].id]
        assert selected.schema_version == 7
        assert selected.payload.type_identifier == "safety.check"
    with ReadOnlySQLiteTraceReader(AnyTraceableEvent, path) as reader:
        reloaded = reader.get_run(first.run_id)
        assert reloaded.capture_quality.complete
        assert reloaded.events[0].schema_version == 7
        assert reloaded.events[0].payload.priority == TracePriority.CRITICAL


@pytest.mark.parametrize("store_kind", ["sqlite", "memory"])
def test_finished_runs_reject_late_event_and_edge_writes(tmp_path, store_kind):
    store = (SQLiteTraceStore(SafetyCheck, str(tmp_path / "trace.sqlite"), start_writer=False)
             if store_kind == "sqlite" else InMemoryTraceStore())
    with store:
        active = record_check(store)
        run = store.prepare_evidence(active.run_id)
        assert run.capture_quality.complete
        event = run.events[0]
        with pytest.raises(RuntimeError, match="closed"):
            active.record(SafetyCheck("late"), "guard")
        with pytest.raises(RuntimeError, match="sealed"):
            store.record(event)
        with pytest.raises(RuntimeError, match="sealed"):
            store.link_for_run(active.run_id, event.id, uuid.uuid4(), TraceEdgeType.DERIVED_FROM)
        assert store.prepare_evidence(active.run_id).capture_quality.attempted_events == 1


def test_concurrent_runs_have_independent_durable_loss_counters(tmp_path):
    path = str(tmp_path / "trace.sqlite")
    with SQLiteTraceStore(SafetyCheck, path, max_global_buffer=1, start_writer=False) as store:
        first, second = uuid.uuid4(), uuid.uuid4()
        store.begin_run(first, "first")
        store.begin_run(second, "second")
        for run_id in (first, second):
            store.record(TraceEvent(run_id, "case", "guard", 1, 0, None, None, SafetyCheck("ok")))
        store.finish_run(first)
        store.finish_run(second)
        assert store.prepare_evidence(first).capture_quality.dropped_events == 1
        assert store.prepare_evidence(second).capture_quality.complete
    with SQLiteTraceStore(SafetyCheck, path, start_writer=False) as reopened:
        assert reopened.prepare_evidence(first).capture_quality.dropped_events == 1
        assert reopened.prepare_evidence(second).capture_quality.attempted_events == 1


def test_corrupt_payload_is_counted_and_exact_bytes_remain_archivable(tmp_path):
    with SQLiteTraceStore(AnyTraceableEvent, str(tmp_path / "trace.sqlite"), start_writer=False) as store:
        active = record_check(store)
        store._db.execute("UPDATE trace_events SET payload=? WHERE run_id=?", (b"\xff\x00", str(active.run_id)))
        run = store.get_run(active.run_id)
        assert run.events == []
        assert run.capture_quality.undecoded_events == 1
        assert not RegressionGate().check(run, run).passed
        snapshot = store.repository_run_snapshot(active.run_id)
        assert snapshot["events"][0]["payload_bytes"] == b"\xff\x00"
        assert not snapshot["run"].capture_quality.complete


def test_legacy_direct_record_is_not_inferred_complete(tmp_path):
    with SQLiteTraceStore(SafetyCheck, str(tmp_path / "trace.sqlite"), start_writer=False) as store:
        run_id = uuid.uuid4()
        store.record(TraceEvent(run_id, "case", "guard", 3, 0, None, None, SafetyCheck("ok")))
        quality = store.prepare_evidence(run_id).capture_quality
        assert quality.lifecycle == "legacy_unknown"
        assert quality.attempted_events is None
        assert not quality.complete


def test_failed_run_keeps_its_evidence_and_failure_status(tmp_path):
    with SQLiteTraceStore(SafetyCheck, str(tmp_path / "trace.sqlite"), start_writer=False) as store:
        kit = DProvenanceKit(SafetyCheck)
        with pytest.raises(ValueError, match="application failure"):
            with kit.run("case", store) as active:
                kit.record(SafetyCheck("started"))
                raise ValueError("application failure")
        run = store.prepare_evidence(active.run_id)
        assert len(run.events) == 1
        assert run.capture_quality.lifecycle == "failed"
        assert not run.capture_quality.complete


def test_edge_capacity_is_bounded_and_loss_survives_reopen(tmp_path):
    path = str(tmp_path / "trace.sqlite")
    with SQLiteTraceStore(SafetyCheck, path, max_global_buffer=3, start_writer=False) as store:
        kit = DProvenanceKit(SafetyCheck)
        with kit.run("case", store) as active:
            ids = [kit.record(SafetyCheck(str(i))) for i in range(3)]
            for source, target in ((0, 1), (0, 2), (1, 2), (0, 1)):
                active.link(ids[source], ids[target], TraceEdgeType.DERIVED_FROM)
        quality = store.prepare_evidence(active.run_id).capture_quality
        assert quality.attempted_edges == 4
        assert quality.dropped_edges == 1
    with SQLiteTraceStore(SafetyCheck, path, start_writer=False) as reopened:
        assert reopened.prepare_evidence(active.run_id).capture_quality.dropped_edges == 1


def test_other_run_can_record_while_a_run_is_finishing(tmp_path, monkeypatch):
    with SQLiteTraceStore(SafetyCheck, str(tmp_path / "trace.sqlite"), start_writer=False) as store:
        first, second = uuid.uuid4(), uuid.uuid4()
        store.begin_run(first, "one")
        store.begin_run(second, "two")
        entered, release, recorded = threading.Event(), threading.Event(), threading.Event()
        real_flush = store._writer.flush

        def delayed_flush():
            entered.set()
            assert release.wait(5)
            real_flush()

        monkeypatch.setattr(store._writer, "flush", delayed_flush)
        finishing = threading.Thread(target=store.finish_run, args=(first,))
        finishing.start()
        try:
            assert entered.wait(5)
            def record():
                store.record(TraceEvent(second, "two", "guard", 1, 0, None, None, SafetyCheck("ok")))
                recorded.set()
            writer = threading.Thread(target=record)
            writer.start()
            assert recorded.wait(2), "record waited on another run's disk barrier"
        finally:
            release.set()
            finishing.join(5)
            monkeypatch.setattr(store._writer, "flush", real_flush)


def test_payload_cannot_override_storage_identity():
    raw = json.dumps({"type": "innocent", "priority": 0}).encode()
    decoded = AnyTraceableEvent.from_storage(raw, "safety.check", 3)
    assert decoded.type_identifier == "safety.check"
    assert decoded.priority == TracePriority.CRITICAL


def test_workflow_cannot_be_bound_after_first_attempt(tmp_path):
    with SQLiteTraceStore(SafetyCheck, str(tmp_path / "trace.sqlite"), start_writer=False) as store:
        kit = DProvenanceKit(SafetyCheck)
        with kit.run("case", store) as active:
            kit.record(SafetyCheck("ok"))
            with pytest.raises(ValueError, match="before recording"):
                store.bind_workflow(active.run_id, "a" * 64)


def test_reader_store_cannot_overwrite_another_stores_live_completion(tmp_path):
    path = str(tmp_path / "capture.sqlite")
    with SQLiteTraceStore(SafetyCheck, path, start_writer=False) as first:
        kit = DProvenanceKit(SafetyCheck)
        with kit.run("one", first) as active:
            kit.record(SafetyCheck("ok"))
            first.flush()
            second = SQLiteTraceStore(SafetyCheck, path, start_writer=False)
        second.close()
    with SQLiteTraceStore(SafetyCheck, path, start_writer=False) as reopened:
        assert reopened.get_run(active.run_id).capture_quality.complete

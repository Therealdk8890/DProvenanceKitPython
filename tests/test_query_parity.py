"""Ports QueryParityTests: the in-memory and SQLite backends must agree on every query."""

from __future__ import annotations

import pytest

from dprovenancekit import DProvenanceKit, InMemoryTraceStore, SQLiteTraceStore, TraceQueryDSL
from conftest import TestEvent


def _matches(scenario, query, temp_db_path, context_id="case"):
    kit = DProvenanceKit(TestEvent)

    mem_store = InMemoryTraceStore()
    with kit.run(context_id=context_id, store=mem_store):
        scenario(kit.record)
    mem = sorted(r.context_id for r in mem_store.query_runs(query))

    sql_store = SQLiteTraceStore(TestEvent, temp_db_path)
    with kit.run(context_id=context_id, store=sql_store):
        scenario(kit.record)
    sql_store.flush()
    sql = sorted(r.context_id for r in sql_store.query_runs(query))

    return mem, sql


def test_before_anchors_to_first_occurrence(temp_db_path):
    def scenario(record):
        record(TestEvent.error_detected())
        record(TestEvent.step_completed(1))
        record(TestEvent.error_detected())

    mem, sql = _matches(
        scenario,
        TraceQueryDSL().requiring_preceded_by("errorDetected", "stepCompleted"),
        temp_db_path,
    )
    assert mem == sql
    assert mem == []


def test_sequence_uses_causal_order_not_timestamp(temp_db_path):
    def scenario(record):
        record(TestEvent.process_started())
        record(TestEvent.error_detected())
        record(TestEvent.process_finished())

    mem, sql = _matches(
        scenario,
        TraceQueryDSL().requiring_sequence(["processStarted", "errorDetected", "processFinished"]),
        temp_db_path,
    )
    assert mem == sql
    assert mem == ["case"]


def test_operator_parity_matrix(tmp_path):
    def scenario(record):
        record(TestEvent.process_started())
        record(TestEvent.step_completed(1))
        record(TestEvent.error_detected())
        record(TestEvent.step_completed(2))
        record(TestEvent.process_finished())

    queries = {
        "contains": TraceQueryDSL().requiring_step("errorDetected"),
        "contains-miss": TraceQueryDSL().requiring_step("rollback"),
        "missing": TraceQueryDSL().missing_step("rollback"),
        "missing-hit": TraceQueryDSL().missing_step("errorDetected"),
        "after": TraceQueryDSL().requiring_followed_by("processStarted", "processFinished"),
        "after-miss": TraceQueryDSL().requiring_followed_by("processFinished", "processStarted"),
        "before": TraceQueryDSL().requiring_preceded_by("errorDetected", "processStarted"),
        "before-miss": TraceQueryDSL().requiring_preceded_by("processStarted", "errorDetected"),
        "sequence": TraceQueryDSL().requiring_sequence(["processStarted", "errorDetected", "processFinished"]),
        "sequence-miss": TraceQueryDSL().requiring_sequence(["processFinished", "processStarted"]),
        "and": TraceQueryDSL().requiring_step("errorDetected").missing_step("rollback"),
        # CountStep: stepCompleted occurs twice in the scenario.
        "count-exact": TraceQueryDSL().requiring_repeated_step("stepCompleted", 2),
        "count-over": TraceQueryDSL().requiring_repeated_step("stepCompleted", 3),
        "count-one": TraceQueryDSL().requiring_repeated_step("errorDetected", 1),
        "count-and": TraceQueryDSL().requiring_repeated_step("stepCompleted", 2).missing_step("rollback"),
    }

    for name, query in queries.items():
        db_path = str(tmp_path / f"{name}.sqlite")
        mem, sql = _matches(scenario, query, db_path)
        assert mem == sql, f"Backend divergence on query: {name}"


def test_count_step_matches_at_threshold_and_excludes_below(temp_db_path):
    def scenario(record):
        record(TestEvent.step_completed(1))
        record(TestEvent.step_completed(2))

    hit = _matches(scenario, TraceQueryDSL().requiring_repeated_step("stepCompleted", 2), temp_db_path)
    assert hit[0] == hit[1] == ["case"]

    miss = _matches(scenario, TraceQueryDSL().requiring_repeated_step("stepCompleted", 3), temp_db_path)
    assert miss[0] == miss[1] == []


def test_count_step_requires_positive_min_count():
    with pytest.raises(ValueError):
        TraceQueryDSL().requiring_repeated_step("stepCompleted", 0)


def test_count_step_is_rejected_by_the_cloud_wire_serializer():
    from dprovenancekit import NotImplementedTraceError
    from dprovenancekit.cloud_store import _serialize_node
    from dprovenancekit.query import CountStep

    # CountStep is a local-backend capability; the cloud wire (Trace Spec v1) must fail loudly
    # rather than silently serialize it to an empty node.
    with pytest.raises(NotImplementedTraceError):
        _serialize_node(CountStep(step="stepCompleted", min_count=2))

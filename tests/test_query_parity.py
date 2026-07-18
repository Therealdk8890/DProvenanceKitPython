"""Ports QueryParityTests: the in-memory and SQLite backends must agree on every query."""

from __future__ import annotations

import pytest

from dprovenancekit import (
    DProvenanceKit,
    InMemoryTraceStore,
    SQLiteTraceStore,
    TraceQueryDSL,
)
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
        TraceQueryDSL().requiring_sequence(
            ["processStarted", "errorDetected", "processFinished"]
        ),
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
        "after": TraceQueryDSL().requiring_followed_by(
            "processStarted", "processFinished"
        ),
        "after-miss": TraceQueryDSL().requiring_followed_by(
            "processFinished", "processStarted"
        ),
        "before": TraceQueryDSL().requiring_preceded_by(
            "errorDetected", "processStarted"
        ),
        "before-miss": TraceQueryDSL().requiring_preceded_by(
            "processStarted", "errorDetected"
        ),
        "sequence": TraceQueryDSL().requiring_sequence(
            ["processStarted", "errorDetected", "processFinished"]
        ),
        "sequence-miss": TraceQueryDSL().requiring_sequence(
            ["processFinished", "processStarted"]
        ),
        "and": TraceQueryDSL().requiring_step("errorDetected").missing_step("rollback"),
        # CountStep: stepCompleted occurs twice in the scenario.
        "count-exact": TraceQueryDSL().requiring_repeated_step("stepCompleted", 2),
        "count-over": TraceQueryDSL().requiring_repeated_step("stepCompleted", 3),
        "count-one": TraceQueryDSL().requiring_repeated_step("errorDetected", 1),
        "count-and": TraceQueryDSL()
        .requiring_repeated_step("stepCompleted", 2)
        .missing_step("rollback"),
    }

    for name, query in queries.items():
        db_path = str(tmp_path / f"{name}.sqlite")
        mem, sql = _matches(scenario, query, db_path)
        assert mem == sql, f"Backend divergence on query: {name}"


def test_count_step_matches_at_threshold_and_excludes_below(temp_db_path):
    def scenario(record):
        record(TestEvent.step_completed(1))
        record(TestEvent.step_completed(2))

    hit = _matches(
        scenario,
        TraceQueryDSL().requiring_repeated_step("stepCompleted", 2),
        temp_db_path,
    )
    assert hit[0] == hit[1] == ["case"]

    miss = _matches(
        scenario,
        TraceQueryDSL().requiring_repeated_step("stepCompleted", 3),
        temp_db_path,
    )
    assert miss[0] == miss[1] == []


def test_count_step_requires_positive_min_count():
    with pytest.raises(ValueError):
        TraceQueryDSL().requiring_repeated_step("stepCompleted", 0)


def test_count_step_engine_scope_parity(tmp_path):
    """Engine-scoped counts must agree across backends and count only that engine."""
    kit = DProvenanceKit(TestEvent)

    def scenario():
        # 2x stepCompleted under 'search', 1x under 'browse', 1x with no engine.
        with kit.with_engine("search"):
            kit.record(TestEvent.step_completed(1))
            kit.record(TestEvent.step_completed(2))
        with kit.with_engine("browse"):
            kit.record(TestEvent.step_completed(3))
        kit.record(TestEvent.step_completed(4))

    mem_store = InMemoryTraceStore()
    with kit.run(context_id="case", store=mem_store):
        scenario()
    sql_store = SQLiteTraceStore(TestEvent, str(tmp_path / "engine-count.sqlite"))
    with kit.run(context_id="case", store=sql_store):
        scenario()
    sql_store.flush()

    queries = {
        # search emitted 2 — matches at 2, not at 3.
        "engine-hit": TraceQueryDSL().requiring_repeated_step(
            "stepCompleted", 2, engine="search"
        ),
        "engine-miss": TraceQueryDSL().requiring_repeated_step(
            "stepCompleted", 3, engine="search"
        ),
        # browse emitted 1 — an unscoped count of 3 would swallow it.
        "engine-other-miss": TraceQueryDSL().requiring_repeated_step(
            "stepCompleted", 2, engine="browse"
        ),
        # absent engine never matches.
        "engine-absent": TraceQueryDSL().requiring_repeated_step(
            "stepCompleted", 1, engine="nope"
        ),
        # unscoped still counts all 4 occurrences.
        "unscoped": TraceQueryDSL().requiring_repeated_step("stepCompleted", 4),
    }
    expected = {
        "engine-hit": ["case"],
        "engine-miss": [],
        "engine-other-miss": [],
        "engine-absent": [],
        "unscoped": ["case"],
    }
    for name, query in queries.items():
        mem = sorted(r.context_id for r in mem_store.query_runs(query))
        sql = sorted(r.context_id for r in sql_store.query_runs(query))
        assert mem == sql == expected[name], f"Backend divergence on query: {name}"


def test_nested_boolean_composition_parity(tmp_path):
    """Compound query members nested below the top level must compile with the AST's
    grouping. SQLite gives UNION/INTERSECT/EXCEPT equal, left-to-right precedence, so a
    naive flat join silently re-groups nested AND/OR/missing_step and diverges from the
    in-memory evaluator. Every case here is discriminating — it returns the wrong runs
    under a flat compilation, so it also pins correctness, not merely backend agreement."""

    def only(*steps):
        def scenario(record):
            for step in steps:
                record(TestEvent(step))

        return scenario

    cases = [
        # has(errorDetected) OR missing(stepCompleted); the run has BOTH steps.
        # Flat: (A UNION runs) EXCEPT stepCompleted == runs - stepCompleted -> run wrongly
        # excluded even though has(errorDetected) already satisfies the OR.
        (
            only("errorDetected", "stepCompleted"),
            TraceQueryDSL()
            .requiring_step("errorDetected")
            .or_(TraceQueryDSL().missing_step("stepCompleted")),
            ["case"],
        ),
        # has(errorDetected) OR ((stepCompleted OR processStarted) AND processFinished);
        # the run has ONLY errorDetected. Flat left-to-right groups as
        # (((ED UNION SC) UNION PS) INTERSECT PF) -> wrongly excluded.
        (
            only("errorDetected"),
            TraceQueryDSL()
            .requiring_step("errorDetected")
            .or_(
                TraceQueryDSL()
                .requiring_step("stepCompleted")
                .or_(TraceQueryDSL().requiring_step("processStarted"))
                .requiring_step("processFinished")
            ),
            ["case"],
        ),
    ]

    for i, (scenario, query, expected) in enumerate(cases):
        db_path = str(tmp_path / f"nested-{i}.sqlite")
        mem, sql = _matches(scenario, query, db_path)
        assert mem == sql == expected, f"case {i}: mem={mem} sql={sql} expected={expected}"

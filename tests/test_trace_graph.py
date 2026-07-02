"""Ports TraceGraphTests: graph validators + store lineage/impact/explain round-trips."""

from __future__ import annotations

import uuid

import pytest

from dprovenancekit import (
    DProvenanceKit,
    InMemoryTraceStore,
    SQLiteTraceStore,
    SelfReferentialEdge,
    StructuralCycleDetected,
    TraceEdge,
    TraceEdgeType,
    TraceEvent,
    TraceExplanation,
    TraceGraph,
    TraceGraphProvenanceValidator,
    TraceGraphValidator,
)
from conftest import TestEvent


def _node(payload, id):
    return TraceEvent(
        id=id,
        run_id=uuid.uuid4(),
        context_id="test",
        engine_name="test",
        schema_version=1,
        sequence=0,
        span_id=None,
        parent_span_id=None,
        payload=payload,
    )


def test_structural_validator_acyclic_graph_passes():
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    graph = TraceGraph(
        nodes={
            a: _node(TestEvent.process_started(), a),
            b: _node(TestEvent.step_completed(1), b),
            c: _node(TestEvent.process_finished(), c),
        },
        edges=[
            TraceEdge(a, b, TraceEdgeType.DERIVED_FROM),
            TraceEdge(b, c, TraceEdgeType.GENERATED_FROM),
        ],
    )
    TraceGraphValidator().validate_structural_integrity(graph)  # no raise


def test_structural_validator_self_edge_throws():
    a = uuid.uuid4()
    graph = TraceGraph(
        nodes={a: _node(TestEvent.process_started(), a)},
        edges=[TraceEdge(a, a, TraceEdgeType.DERIVED_FROM)],
    )
    with pytest.raises(SelfReferentialEdge):
        TraceGraphValidator().validate_structural_integrity(graph)


def test_structural_validator_cycle_throws():
    a, b = uuid.uuid4(), uuid.uuid4()
    graph = TraceGraph(
        nodes={
            a: _node(TestEvent.process_started(), a),
            b: _node(TestEvent.process_finished(), b),
        },
        edges=[
            TraceEdge(a, b, TraceEdgeType.DERIVED_FROM),
            TraceEdge(b, a, TraceEdgeType.DERIVED_FROM),
        ],
    )
    with pytest.raises(StructuralCycleDetected):
        TraceGraphValidator().validate_structural_integrity(graph)


def test_provenance_validator_flags_orphan_section_and_unused_fact():
    fact, section = uuid.uuid4(), uuid.uuid4()
    graph = TraceGraph(
        nodes={
            fact: _node(TestEvent.process_started(), fact),
            section: _node(TestEvent.process_finished(), section),
        },
        edges=[],
    )
    validator = TraceGraphProvenanceValidator(
        generated_section_identifier="processFinished",
        fact_extracted_identifier="processStarted",
    )
    anomalies = validator.detect_anomalies(graph)
    assert len(anomalies) == 2
    assert any("Orphan generated section" in a for a in anomalies)
    assert any("Unused extracted fact" in a for a in anomalies)


def test_provenance_validator_clean_graph_no_anomalies():
    fact, section = uuid.uuid4(), uuid.uuid4()
    graph = TraceGraph(
        nodes={
            fact: _node(TestEvent.process_started(), fact),
            section: _node(TestEvent.process_finished(), section),
        },
        edges=[TraceEdge(fact, section, TraceEdgeType.INFORMED)],
    )
    validator = TraceGraphProvenanceValidator(
        generated_section_identifier="processFinished",
        fact_extracted_identifier="processStarted",
    )
    assert validator.detect_anomalies(graph) == []


def test_in_memory_store_lineage_impact_explain():
    store = InMemoryTraceStore()
    kit = DProvenanceKit(TestEvent)
    with kit.run(context_id="g", store=store):
        a = kit.record(TestEvent.process_started())
        b = kit.record(TestEvent.step_completed(1))
        c = kit.record(TestEvent.process_finished())
        kit.link(a, b, TraceEdgeType.INFORMED)
        kit.link(b, c, TraceEdgeType.DERIVED_FROM)
    store.flush()

    lineage = store.lineage(c)
    assert len(lineage.edges) == 2
    assert set(lineage.nodes.keys()) == {a, b, c}

    impact = store.impact(a)
    assert len(impact.edges) == 2

    explanation = store.explain(c)
    assert len(explanation.derived_from) == 1
    assert explanation.informed_by == []


def test_sqlite_store_lineage_and_impact_round_trip(temp_db_path):
    store = SQLiteTraceStore(TestEvent, temp_db_path)
    kit = DProvenanceKit(TestEvent)
    with kit.run(context_id="g", store=store):
        a = kit.record(TestEvent.process_started())
        b = kit.record(TestEvent.process_finished())
        kit.link(a, b, TraceEdgeType.DERIVED_FROM)
    store.flush()

    lineage = store.lineage(b)
    assert lineage.edges == [TraceEdge(a, b, TraceEdgeType.DERIVED_FROM)]
    assert set(lineage.nodes.keys()) == {a, b}

    impact = store.impact(a)
    assert len(impact.edges) == 1


def test_sqlite_store_cyclic_edges_traversal_terminates(temp_db_path):
    store = SQLiteTraceStore(TestEvent, temp_db_path)
    kit = DProvenanceKit(TestEvent)
    with kit.run(context_id="cycle", store=store):
        a = kit.record(TestEvent.process_started())
        b = kit.record(TestEvent.process_finished())
        kit.link(a, b, TraceEdgeType.DERIVED_FROM)
        kit.link(b, a, TraceEdgeType.DERIVED_FROM)  # cycle
    store.flush()

    assert len(store.lineage_edges(a)) == 2
    assert len(store.impact_edges(a)) == 2


def test_link_rejects_self_referential_edge():
    store = InMemoryTraceStore()
    kit = DProvenanceKit(TestEvent)
    with kit.run(context_id="self", store=store):
        a = kit.record(TestEvent.process_started())
        kit.link(a, a, TraceEdgeType.DERIVED_FROM)
    store.flush()
    assert store.lineage_edges(a) == []


def _record_chain(store):
    """a -> b -> c -> d, returning the four event ids."""
    kit = DProvenanceKit(TestEvent)
    with kit.run(context_id="chain", store=store):
        a = kit.record(TestEvent.process_started())
        b = kit.record(TestEvent.step_completed(1))
        c = kit.record(TestEvent.step_completed(2))
        d = kit.record(TestEvent.process_finished())
        kit.link(a, b, TraceEdgeType.DERIVED_FROM)
        kit.link(b, c, TraceEdgeType.DERIVED_FROM)
        kit.link(c, d, TraceEdgeType.DERIVED_FROM)
    store.flush()
    return a, b, c, d


@pytest.fixture(params=["memory", "sqlite"])
def chain_store(request, temp_db_path):
    if request.param == "memory":
        store = InMemoryTraceStore()
    else:
        store = SQLiteTraceStore(TestEvent, temp_db_path)
    yield store


def test_lineage_max_depth_bounds_the_walk(chain_store):
    a, b, c, d = _record_chain(chain_store)

    assert len(chain_store.lineage_edges(d, max_depth=1)) == 1
    assert len(chain_store.lineage_edges(d, max_depth=2)) == 2
    assert len(chain_store.lineage_edges(d, max_depth=3)) == 3
    assert len(chain_store.lineage_edges(d, max_depth=10)) == 3
    assert len(chain_store.lineage_edges(d)) == 3

    shallow = chain_store.lineage(d, max_depth=1)
    assert shallow.edges == [TraceEdge(c, d, TraceEdgeType.DERIVED_FROM)]
    assert set(shallow.nodes.keys()) == {c, d}


def test_impact_max_depth_bounds_the_walk(chain_store):
    a, b, c, d = _record_chain(chain_store)

    assert len(chain_store.impact_edges(a, max_depth=1)) == 1
    assert len(chain_store.impact_edges(a, max_depth=2)) == 2
    assert len(chain_store.impact_edges(a)) == 3

    shallow = chain_store.impact(a, max_depth=1)
    assert shallow.edges == [TraceEdge(a, b, TraceEdgeType.DERIVED_FROM)]
    assert set(shallow.nodes.keys()) == {a, b}


def test_max_depth_zero_returns_just_the_root(chain_store):
    a, b, c, d = _record_chain(chain_store)

    assert chain_store.lineage_edges(d, max_depth=0) == []
    graph = chain_store.lineage(d, max_depth=0)
    assert graph.edges == []
    assert set(graph.nodes.keys()) == {d}
    assert chain_store.impact_edges(a, max_depth=0) == []


def test_negative_max_depth_raises(chain_store):
    a, b, c, d = _record_chain(chain_store)

    with pytest.raises(ValueError):
        chain_store.lineage_edges(d, max_depth=-1)
    with pytest.raises(ValueError):
        chain_store.impact(a, max_depth=-1)


def test_max_depth_terminates_and_dedups_on_cycles(chain_store):
    kit = DProvenanceKit(TestEvent)
    with kit.run(context_id="cycle", store=chain_store):
        a = kit.record(TestEvent.process_started())
        b = kit.record(TestEvent.process_finished())
        kit.link(a, b, TraceEdgeType.DERIVED_FROM)
        kit.link(b, a, TraceEdgeType.DERIVED_FROM)  # cycle
    chain_store.flush()

    # A depth cap far past the cycle length must not duplicate edges.
    assert len(chain_store.lineage_edges(a, max_depth=50)) == 2
    assert len(chain_store.impact_edges(a, max_depth=50)) == 2
    assert len(chain_store.lineage_edges(a, max_depth=1)) == 1


def test_explain_unaffected_by_deep_ancestry(chain_store):
    a, b, c, d = _record_chain(chain_store)

    explanation = chain_store.explain(d)
    assert len(explanation.derived_from) == 1
    assert explanation.informed_by == []


def test_trace_explanation_formatting():
    explanation = TraceExplanation(
        target_node_id=uuid.uuid4(),
        target_node_summary="Generated demand paragraph",
        informed_by=["fact: amount owed"],
        derived_from=["evidence: invoice"],
    )
    text = explanation.formatted()
    assert "Generated demand paragraph" in text
    assert "Informed By:" in text
    assert "fact: amount owed" in text
    assert "Derived From:" in text
    assert "evidence: invoice" in text

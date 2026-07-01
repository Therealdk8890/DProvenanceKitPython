import uuid
import pytest
from dprovenancekit import TraceGraph, TraceEdge, TraceEdgeType, InMemoryTraceStore
from dprovenancekit.visualizer import render_trace_html
from conftest import TestEvent

def _node(payload, id):
    from dprovenancekit import TraceEvent
    return TraceEvent(
        id=id,
        run_id=uuid.uuid4(),
        context_id="test",
        engine_name="test_engine",
        schema_version=1,
        sequence=1,
        span_id=None,
        parent_span_id=None,
        payload=payload,
    )

def test_visualizer_renders_html():
    n1 = uuid.uuid4()
    n2 = uuid.uuid4()
    graph = TraceGraph(
        nodes={
            n1: _node(TestEvent.process_started(), n1),
            n2: _node(TestEvent.process_finished(), n2),
        },
        edges=[TraceEdge(n1, n2, TraceEdgeType.DERIVED_FROM)],
    )
    
    html = render_trace_html(graph, title="Test UI")
    assert "Test UI" in html
    assert "test_engine" in html
    assert "processStarted" in html
    assert "processFinished" in html
    assert str(n1) in html
    assert str(n2) in html
    
    # Check that javascript block is present
    assert "window.graphData =" in html
    assert "function selectNode" in html

import uuid
from dprovenancekit import TraceGraph, TraceEdge, TraceEdgeType
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


def test_visualizer_escapes_malicious_trace_data():
    """Trace payloads/engine names are attacker-influenced (LLM/tool output). They must be
    neutralized both in the server-rendered HTML and inside the inlined <script> JSON, so a
    ``</script>`` in the data cannot break out and execute — the stored-XSS class the 0.6.1
    security release fixed in index.html/report.py but originally missed here."""
    from dataclasses import dataclass
    from dprovenancekit import TraceableEvent, TracePriority

    breakout = "</script><script>alert(1)</script>"

    @dataclass(frozen=True)
    class Evil(TraceableEvent):
        @property
        def type_identifier(self) -> str:
            return breakout

        @property
        def priority(self) -> TracePriority:
            return TracePriority.STRUCTURAL

        def to_dict(self) -> dict:
            return {"data": breakout}

    from dprovenancekit import TraceEvent

    nid = uuid.uuid4()
    # Inject via engine_name too (also interpolated into the timeline HTML).
    node = TraceEvent(
        id=nid,
        run_id=uuid.uuid4(),
        context_id="test",
        engine_name=breakout,
        schema_version=1,
        sequence=1,
        span_id=None,
        parent_span_id=None,
        payload=Evil(),
    )
    graph = TraceGraph(nodes={nid: node}, edges=[])

    html = render_trace_html(graph, title=breakout)

    # The raw breakout string must never appear verbatim, and there must be exactly one
    # real closing </script> tag (the document's own), not one smuggled in via the data.
    assert breakout not in html
    assert html.count("</script>") == 1
    # Data reaches the page in escaped form: < in the script JSON, &lt; in the HTML.
    assert "\\u003c" in html
    assert "&lt;/script&gt;" in html

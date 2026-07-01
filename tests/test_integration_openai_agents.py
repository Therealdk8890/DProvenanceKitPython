"""Tests for the OpenAI Agents SDK integration.

The SDK drives a ``TracingProcessor`` by calling ``on_trace_start`` / ``on_span_start`` /
``on_span_end`` / ``on_trace_end`` with ``Trace`` and ``Span`` objects. We exercise the
processor the same way — with small stand-ins carrying the attributes the SDK provides —
so the full translation is verified without installing ``openai-agents``. A final test
feeds *real* ``SpanData`` objects through the processor when the SDK is installed.
"""

from __future__ import annotations

import tempfile
import uuid

import pytest

from dprovenancekit import (
    InMemoryTraceStore,
    SQLiteTraceStore,
    TracePriority,
)
from dprovenancekit.edge import TraceEdgeType
from dprovenancekit.integrations.openai_agents import (
    DProvenanceTracingProcessor,
    OpenAIAgentsTraceEvent,
)


# ── Stand-ins for the SDK's Trace / Span / SpanData ─────────────────────────────


class FakeSpanData:
    def __init__(self, type_: str, **fields):
        self.type = type_
        for k, v in fields.items():
            setattr(self, k, v)


class FakeSpan:
    def __init__(self, span_id, trace_id, span_data, *, parent_id=None, error=None):
        self.span_id = span_id
        self.trace_id = trace_id
        self.parent_id = parent_id
        self.span_data = span_data
        self.error = error


class FakeTrace:
    def __init__(self, trace_id, name):
        self.trace_id = trace_id
        self.name = name


def _events_by_type(run):
    return {e.payload.type_identifier: e for e in run.events}


def drive_agent_trace(proc, *, trace_id="trace-1", name="research-run"):
    """A nested trace: agent → (generation, function), as the SDK would emit it.

    Returns the DProvenanceKit run id (captured before on_trace_end pops the run).
    """
    trace = FakeTrace(trace_id, name)
    proc.on_trace_start(trace)

    agent = FakeSpan(
        "s_agent", trace_id,
        FakeSpanData("agent", name="Researcher", tools=["search"], handoffs=[], output_type=None),
    )
    proc.on_span_start(agent)

    gen = FakeSpan(
        "s_gen", trace_id,
        FakeSpanData("generation", model="gpt-4o", input="prompt", output="answer",
                     usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}),
        parent_id="s_agent",
    )
    proc.on_span_start(gen)
    proc.on_span_end(gen)

    fn = FakeSpan(
        "s_fn", trace_id,
        FakeSpanData("function", name="search", input="kittens", output="3 results"),
        parent_id="s_agent",
    )
    proc.on_span_start(fn)
    proc.on_span_end(fn)

    proc.on_span_end(agent)
    run_id = proc.run_id_for(trace_id)
    proc.on_trace_end(trace)
    return run_id


# ── Event type ──────────────────────────────────────────────────────────────────


def test_event_roundtrip_and_canonical_encoding():
    ev = OpenAIAgentsTraceEvent.make(
        "function.end", TracePriority.STRUCTURAL, {"name": "search", "output": "r"}
    )
    assert ev.type_identifier == "function.end"
    assert ev.priority is TracePriority.STRUCTURAL
    assert ev.encode().decode() == '{"name": "search", "output": "r", "priority": 2, "type": "function.end"}'
    assert OpenAIAgentsTraceEvent.decode(ev.encode()) == ev


# ── Trace → run + span tree ─────────────────────────────────────────────────────


def test_trace_with_nested_spans_records_run_and_span_tree():
    store = InMemoryTraceStore()
    proc = DProvenanceTracingProcessor(store)
    run_id = drive_agent_trace(proc, name="research-run")

    run = store.get_run(run_id)
    assert run.context_id == "research-run"
    assert [e.payload.type_identifier for e in run.events] == [
        "agent.start",
        "generation.start",
        "generation.end",
        "function.start",
        "function.end",
        "agent.end",
    ]

    by_type = _events_by_type(run)
    # span_id / parent_id become the span tree.
    assert by_type["agent.start"].span_id == "s_agent"
    assert by_type["agent.start"].parent_span_id is None
    assert by_type["generation.start"].span_id == "s_gen"
    assert by_type["generation.start"].parent_span_id == "s_agent"
    # The active component becomes the engine.
    assert by_type["agent.start"].engine_name == "Researcher"
    assert by_type["generation.start"].engine_name == "gpt-4o"
    assert by_type["function.start"].engine_name == "search"
    # Generation metadata is captured.
    assert by_type["generation.end"].payload.attributes["total_tokens"] == 15
    assert by_type["generation.end"].payload.attributes["model"] == "gpt-4o"
    assert by_type["agent.start"].payload.attributes["tools"] == ["search"]


# ── Lineage edges ─────────────────────────────────────────────────────────────


def test_lifecycle_edges_link_completion_to_start_and_child_to_parent():
    store = InMemoryTraceStore()
    proc = DProvenanceTracingProcessor(store)
    run_id = drive_agent_trace(proc)

    run = store.get_run(run_id)
    by_type = _events_by_type(run)
    gen_start = by_type["generation.start"]
    gen_end = by_type["generation.end"]
    agent_start = by_type["agent.start"]

    incoming = {(e.source_id, e.type) for e in store.lineage_edges(gen_end.id)}
    assert (gen_start.id, TraceEdgeType.DERIVED_FROM) in incoming
    assert (agent_start.id, TraceEdgeType.INFORMED) in incoming


def test_link_lifecycle_off_produces_no_edges():
    store = InMemoryTraceStore()
    proc = DProvenanceTracingProcessor(store, link_lifecycle=False)
    run_id = drive_agent_trace(proc)
    run = store.get_run(run_id)
    by_type = _events_by_type(run)
    assert store.lineage_edges(by_type["generation.end"].id) == []


# ── Errors / guardrails / options ───────────────────────────────────────────────


def test_span_error_is_recorded_as_critical():
    store = InMemoryTraceStore()
    proc = DProvenanceTracingProcessor(store)
    trace = FakeTrace("t", "run")
    proc.on_trace_start(trace)
    span = FakeSpan("s1", "t", FakeSpanData("function", name="search"),
                    error={"message": "tool exploded", "data": {"code": 500}})
    proc.on_span_start(span)
    proc.on_span_end(span)
    run_id = proc.run_id_for("t")
    proc.on_trace_end(trace)

    by_type = _events_by_type(store.get_run(run_id))
    err = by_type["function.error"]
    assert err.payload.priority is TracePriority.CRITICAL
    assert err.payload.attributes["message"] == "tool exploded"
    assert err.payload.attributes["data_keys"] == ["code"]


def test_triggered_guardrail_is_critical():
    store = InMemoryTraceStore()
    proc = DProvenanceTracingProcessor(store)
    trace = FakeTrace("t", "run")
    proc.on_trace_start(trace)
    span = FakeSpan("s1", "t", FakeSpanData("guardrail", name="jailbreak", triggered=True))
    proc.on_span_start(span)
    proc.on_span_end(span)
    run_id = proc.run_id_for("t")
    proc.on_trace_end(trace)

    by_type = _events_by_type(store.get_run(run_id))
    assert by_type["guardrail.end"].payload.priority is TracePriority.CRITICAL
    assert by_type["guardrail.end"].payload.attributes["triggered"] is True


def test_capture_payloads_off_omits_io():
    store = InMemoryTraceStore()
    proc = DProvenanceTracingProcessor(store, capture_payloads=False)
    trace = FakeTrace("t", "run")
    proc.on_trace_start(trace)
    span = FakeSpan("s1", "t", FakeSpanData("function", name="search", input="secret", output="secret"))
    proc.on_span_start(span)
    proc.on_span_end(span)
    run_id = proc.run_id_for("t")
    proc.on_trace_end(trace)

    by_type = _events_by_type(store.get_run(run_id))
    assert "input" not in by_type["function.start"].payload.attributes
    assert "output" not in by_type["function.end"].payload.attributes
    # The name (structural metadata) is still present.
    assert by_type["function.start"].payload.attributes["name"] == "search"


# ── Concurrency / flush ──────────────────────────────────────────────────────────


def test_concurrent_traces_route_to_separate_runs():
    store = InMemoryTraceStore()
    proc = DProvenanceTracingProcessor(store)
    t1, t2 = FakeTrace("t1", "run-A"), FakeTrace("t2", "run-B")
    proc.on_trace_start(t1)
    proc.on_trace_start(t2)
    # Interleave spans from the two traces.
    a = FakeSpan("a", "t1", FakeSpanData("agent", name="A"))
    b = FakeSpan("b", "t2", FakeSpanData("agent", name="B"))
    proc.on_span_start(a)
    proc.on_span_start(b)
    proc.on_span_end(b)
    proc.on_span_end(a)
    rid1, rid2 = proc.run_id_for("t1"), proc.run_id_for("t2")
    proc.on_trace_end(t1)
    proc.on_trace_end(t2)

    run1, run2 = store.get_run(rid1), store.get_run(rid2)
    assert run1.context_id == "run-A" and run2.context_id == "run-B"
    assert run1.events[0].engine_name == "A"
    assert run2.events[0].engine_name == "B"
    # No cross-contamination.
    assert all(e.run_id == rid1 for e in run1.events)


def test_force_flush_writes_open_trace():
    with tempfile.TemporaryDirectory() as tmp:
        store = SQLiteTraceStore(OpenAIAgentsTraceEvent, f"{tmp}/t.sqlite", start_writer=False)
        proc = DProvenanceTracingProcessor(store)
        trace = FakeTrace("t", "run")
        proc.on_trace_start(trace)
        span = FakeSpan("s1", "t", FakeSpanData("agent", name="A"))
        proc.on_span_start(span)
        run_id = proc.run_id_for("t")
        proc.force_flush()  # without on_trace_end
        rows = store._db.query("SELECT event_count FROM runs WHERE run_id = ?", (str(run_id),))
    assert rows and rows[0][0] == 1


# ── Fingerprint: structural identity of an agent's path ─────────────────────────


def _fingerprint_after(store: SQLiteTraceStore, run_id: uuid.UUID) -> str:
    store.flush()
    rows = store._db.query("SELECT fingerprint FROM runs WHERE run_id = ?", (str(run_id),))
    return rows[0][0]


def test_same_path_shares_fingerprint_divergent_path_differs():
    with tempfile.TemporaryDirectory() as tmp:
        store = SQLiteTraceStore(OpenAIAgentsTraceEvent, f"{tmp}/t.sqlite", start_writer=False)
        proc = DProvenanceTracingProcessor(store)

        a = drive_agent_trace(proc, trace_id="a", name="a")
        b = drive_agent_trace(proc, trace_id="b", name="b")

        # Divergent: a function runs before the generation.
        trace = FakeTrace("c", "c")
        proc.on_trace_start(trace)
        agent = FakeSpan("s_agent", "c", FakeSpanData("agent", name="Researcher", tools=["search"], handoffs=[]))
        proc.on_span_start(agent)
        fn = FakeSpan("s_fn", "c", FakeSpanData("function", name="search", input="kittens", output="3 results"), parent_id="s_agent")
        proc.on_span_start(fn)
        proc.on_span_end(fn)
        gen = FakeSpan("s_gen", "c", FakeSpanData("generation", model="gpt-4o", input="prompt", output="answer",
                       usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}), parent_id="s_agent")
        proc.on_span_start(gen)
        proc.on_span_end(gen)
        proc.on_span_end(agent)
        c = proc.run_id_for("c")
        proc.on_trace_end(trace)

        store.flush()
        fp_a, fp_b, fp_c = _fingerprint_after(store, a), _fingerprint_after(store, b), _fingerprint_after(store, c)

    assert fp_a == fp_b
    assert fp_a != fp_c


# ── Hardening: behaviors confirmed by adversarial review ────────────────────────


def test_response_span_uses_model_as_engine():
    # The Responses API (the SDK default) emits `response` spans; the model lives on the
    # nested Response object, and must still become the engine.
    store = InMemoryTraceStore()
    proc = DProvenanceTracingProcessor(store)
    trace = FakeTrace("t", "run")
    proc.on_trace_start(trace)
    resp = type("Resp", (), {"model": "gpt-4o-mini", "id": "resp_1"})()
    span = FakeSpan("s1", "t", FakeSpanData("response", response=resp, usage={"total_tokens": 9}))
    proc.on_span_start(span)
    proc.on_span_end(span)
    run_id = proc.run_id_for("t")
    proc.on_trace_end(trace)

    by_type = _events_by_type(store.get_run(run_id))
    assert by_type["response.start"].engine_name == "gpt-4o-mini"
    assert by_type["response.end"].payload.attributes["model"] == "gpt-4o-mini"
    assert by_type["response.end"].payload.attributes["response_id"] == "resp_1"


def test_link_off_does_not_populate_or_leak_state():
    store = InMemoryTraceStore()
    proc = DProvenanceTracingProcessor(store, link_lifecycle=False)
    trace = FakeTrace("t", "run")
    proc.on_trace_start(trace)
    span = FakeSpan("s1", "t", FakeSpanData("agent", name="A"))
    proc.on_span_start(span)
    # With edges off, start-event bookkeeping is not even populated...
    assert proc._traces["t"].start_events == {}
    proc.on_span_end(span)
    proc.on_trace_end(trace)
    # ...and per-trace state is dropped entirely when the trace ends (no global leak).
    assert proc._traces == {}


def test_concurrent_traces_with_colliding_span_ids_keep_edges_isolated():
    store = InMemoryTraceStore()
    proc = DProvenanceTracingProcessor(store)
    t1, t2 = FakeTrace("t1", "A"), FakeTrace("t2", "B")
    proc.on_trace_start(t1)
    proc.on_trace_start(t2)
    # Both traces deliberately reuse the SAME span ids.
    proc.on_span_start(FakeSpan("s_parent", "t1", FakeSpanData("agent", name="A")))
    proc.on_span_start(FakeSpan("s_parent", "t2", FakeSpanData("agent", name="B")))
    proc.on_span_start(FakeSpan("s_child", "t1", FakeSpanData("function", name="f1"), parent_id="s_parent"))
    proc.on_span_start(FakeSpan("s_child", "t2", FakeSpanData("function", name="f2"), parent_id="s_parent"))
    rid1, rid2 = proc.run_id_for("t1"), proc.run_id_for("t2")
    proc.on_trace_end(t1)
    proc.on_trace_end(t2)

    run1, run2 = store.get_run(rid1), store.get_run(rid2)
    ids1 = {e.id for e in run1.events}
    ids2 = {e.id for e in run2.events}
    by1, by2 = _events_by_type(run1), _events_by_type(run2)
    # The child's INFORMED edge points at its OWN run's parent, never the other trace's.
    sources1 = {e.source_id for e in store.lineage_edges(by1["function.start"].id)}
    sources2 = {e.source_id for e in store.lineage_edges(by2["function.start"].id)}
    assert by1["agent.start"].id in sources1 and sources1 <= ids1
    assert by2["agent.start"].id in sources2 and sources2 <= ids2


def test_duplicate_trace_start_keeps_first_run():
    store = InMemoryTraceStore()
    proc = DProvenanceTracingProcessor(store)
    trace = FakeTrace("t", "run")
    proc.on_trace_start(trace)
    rid_first = proc.run_id_for("t")
    proc.on_trace_start(trace)  # duplicate start for a live trace
    assert proc.run_id_for("t") == rid_first


def test_duplicate_span_end_is_idempotent():
    store = InMemoryTraceStore()
    proc = DProvenanceTracingProcessor(store)
    trace = FakeTrace("t", "run")
    proc.on_trace_start(trace)
    span = FakeSpan("s1", "t", FakeSpanData("function", name="search"))
    proc.on_span_start(span)
    proc.on_span_end(span)
    proc.on_span_end(span)  # repeated end — must not double-emit
    run_id = proc.run_id_for("t")
    proc.on_trace_end(trace)

    types = [e.payload.type_identifier for e in store.get_run(run_id).events]
    assert types == ["function.start", "function.end"]


# ── Real SpanData, when the SDK is installed ─────────────────────────────────────


def test_real_span_data_objects_are_handled():
    pytest.importorskip("agents")
    from agents.tracing.processor_interface import TracingProcessor
    from agents.tracing.span_data import FunctionSpanData

    store = InMemoryTraceStore()
    proc = DProvenanceTracingProcessor(store)
    assert isinstance(proc, TracingProcessor)  # we are a real processor

    trace = FakeTrace("t", "run")
    proc.on_trace_start(trace)
    span = FakeSpan("s1", "t", FunctionSpanData(name="search", input="q", output="r"))
    proc.on_span_start(span)
    proc.on_span_end(span)
    run_id = proc.run_id_for("t")
    proc.on_trace_end(trace)

    by_type = _events_by_type(store.get_run(run_id))
    assert "function.start" in by_type and "function.end" in by_type
    assert by_type["function.start"].engine_name == "search"

# git-blob-rewrite

"""Tests for the LangChain integration.

LangChain invokes a callback handler by calling its ``on_*`` methods with ``run_id`` /
``parent_run_id`` keyword arguments. We exercise the handler the same way — driving the
callbacks directly — so the full translation (events, span tree, lineage edges,
priorities, querying, fingerprint) is verified without importing ``langchain-core``.
A final test runs a real LangChain ``Runnable`` when the package is installed.
"""

from __future__ import annotations

import tempfile
import uuid
from typing import List, Optional

import pytest

from dprovenancekit import (
    InMemoryTraceStore,
    SQLiteTraceStore,
    TracePriority,
    TraceQueryDSL,
)
from dprovenancekit.edge import TraceEdgeType
from dprovenancekit.integrations.langchain import (
    DProvenanceCallbackHandler,
    DProvenanceTracer,
    LangChainTraceEvent,
    LCEventType,
)


# ── Minimal stand-ins for the LangChain objects the callbacks receive ───────────


class _FakeGeneration:
    def __init__(self, text: str):
        self.text = text


class _FakeLLMResult:
    """Mimics langchain_core.outputs.LLMResult enough for attribute extraction."""

    def __init__(self, text: str = "hello", model_name: str = "gpt-test"):
        self.generations = [[_FakeGeneration(text)]]
        self.llm_output = {
            "model_name": model_name,
            "token_usage": {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12},
        }


class _FakeAgentAction:
    def __init__(self, tool: str, tool_input: str):
        self.tool = tool
        self.tool_input = tool_input
        self.log = f"invoking {tool}"


class _FakeAgentFinish:
    def __init__(self, output: str):
        self.return_values = {"output": output}
        self.log = "done"


# ── Helpers ─────────────────────────────────────────────────────────────────────


def _events_by_type(run):
    return {e.payload.type_identifier: e for e in run.events}


def _drive_rag_run(handler: DProvenanceCallbackHandler):
    """A realistic nested sequence: chain → (retriever, llm), as LangChain would emit it.

    Returns the (root, retriever, llm) LangChain run_ids used.
    """
    root, retr, llm = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    handler.on_chain_start({"name": "RagChain"}, {"question": "q"}, run_id=root, parent_run_id=None)
    handler.on_retriever_start(
        {"id": ["langchain", "retrievers", "MyRetriever"]}, "q", run_id=retr, parent_run_id=root
    )
    handler.on_retriever_end([object(), object(), object()], run_id=retr, parent_run_id=root)
    handler.on_llm_start(
        {"id": ["langchain", "chat_models", "openai", "ChatOpenAI"]},
        ["Context: ...\nQ: q"],
        run_id=llm,
        parent_run_id=root,
    )
    handler.on_llm_end(_FakeLLMResult(text="the answer"), run_id=llm, parent_run_id=root)
    handler.on_chain_end({"answer": "the answer"}, run_id=root, parent_run_id=None)
    return root, retr, llm


# ── Event type ──────────────────────────────────────────────────────────────────


def test_event_roundtrip_and_canonical_encoding():
    ev = LangChainTraceEvent.make(
        LCEventType.TOOL_STARTED, TracePriority.STRUCTURAL, {"tool": "search", "input": "x"}
    )
    assert ev.type_identifier == "toolStarted"
    assert ev.priority is TracePriority.STRUCTURAL
    assert ev.attributes == {"tool": "search", "input": "x"}
    # Canonical (sorted-key) encoding, per Trace Spec v1 section 2.
    assert ev.encode().decode() == '{"input": "x", "priority": 2, "tool": "search", "type": "toolStarted"}'
    # Round-trips back to an equal event.
    assert LangChainTraceEvent.decode(ev.encode()) == ev


def test_event_equality_is_attribute_order_independent():
    a = LangChainTraceEvent.make(LCEventType.LLM_STARTED, TracePriority.STRUCTURAL, {"a": 1, "b": 2})
    b = LangChainTraceEvent.make(LCEventType.LLM_STARTED, TracePriority.STRUCTURAL, {"b": 2, "a": 1})
    assert a == b
    assert hash(a) == hash(b)


def test_make_drops_none_valued_attributes():
    ev = LangChainTraceEvent.make(
        LCEventType.RETRIEVER_ENDED, TracePriority.STRUCTURAL, {"document_count": None}
    )
    assert ev.attributes == {}


# ── Basic recording ─────────────────────────────────────────────────────────────


def test_single_llm_call_records_two_events():
    store = InMemoryTraceStore()
    tracer = DProvenanceTracer(store)
    with tracer.trace(context_id="case-1") as cb:
        rid = uuid.uuid4()
        cb.on_llm_start({"id": ["x", "ChatOpenAI"]}, ["hi"], run_id=rid, parent_run_id=None)
        cb.on_llm_end(_FakeLLMResult(model_name="gpt-test"), run_id=rid, parent_run_id=None)
        run_id = cb.run_id

    run = store.get_run(run_id)
    assert run is not None
    assert run.context_id == "case-1"
    types = [e.payload.type_identifier for e in run.events]
    assert types == [LCEventType.LLM_STARTED, LCEventType.LLM_ENDED]
    # Sequence is monotonic in commit order.
    assert [e.sequence for e in run.events] == [0, 1]
    started, ended = run.events
    assert started.engine_name == "ChatOpenAI"
    assert ended.engine_name == "gpt-test"  # taken from llm_output.model_name
    assert ended.payload.attributes["total_tokens"] == 12
    assert ended.payload.attributes["completion_preview"] == "hello"


# ── Span tree ─────────────────────────────────────────────────────────────────


def test_span_tree_mirrors_langchain_run_tree():
    store = InMemoryTraceStore()
    tracer = DProvenanceTracer(store)
    with tracer.trace(context_id="rag") as cb:
        root, retr, llm = _drive_rag_run(cb)
        run_id = cb.run_id

    run = store.get_run(run_id)
    by_type = _events_by_type(run)

    # run_id → span_id, parent_run_id → parent_span_id.
    assert by_type[LCEventType.CHAIN_STARTED].span_id == str(root)
    assert by_type[LCEventType.CHAIN_STARTED].parent_span_id is None
    assert by_type[LCEventType.RETRIEVER_STARTED].span_id == str(retr)
    assert by_type[LCEventType.RETRIEVER_STARTED].parent_span_id == str(root)
    assert by_type[LCEventType.LLM_STARTED].span_id == str(llm)
    assert by_type[LCEventType.LLM_STARTED].parent_span_id == str(root)
    # Execution order preserved.
    assert [e.payload.type_identifier for e in run.events] == [
        LCEventType.CHAIN_STARTED,
        LCEventType.RETRIEVER_STARTED,
        LCEventType.RETRIEVER_ENDED,
        LCEventType.LLM_STARTED,
        LCEventType.LLM_ENDED,
        LCEventType.CHAIN_ENDED,
    ]
    assert by_type[LCEventType.RETRIEVER_ENDED].payload.attributes["document_count"] == 3


# ── Lineage edges ─────────────────────────────────────────────────────────────


def test_lifecycle_edges_link_completion_to_start_and_child_to_parent():
    store = InMemoryTraceStore()
    tracer = DProvenanceTracer(store)
    with tracer.trace(context_id="rag") as cb:
        _drive_rag_run(cb)
        run_id = cb.run_id

    run = store.get_run(run_id)
    by_type = _events_by_type(run)
    llm_start = by_type[LCEventType.LLM_STARTED]
    llm_end = by_type[LCEventType.LLM_ENDED]
    chain_start = by_type[LCEventType.CHAIN_STARTED]

    # The completion is DERIVED_FROM its start; the child step is INFORMED by its parent.
    incoming = {(e.source_id, e.type) for e in store.lineage_edges(llm_end.id)}
    assert (llm_start.id, TraceEdgeType.DERIVED_FROM) in incoming
    assert (chain_start.id, TraceEdgeType.INFORMED) in incoming


def test_link_lifecycle_off_produces_no_edges():
    store = InMemoryTraceStore()
    tracer = DProvenanceTracer(store)
    with tracer.trace(context_id="rag", link_lifecycle=False) as cb:
        _, _, llm = _drive_rag_run(cb)
        run_id = cb.run_id

    run = store.get_run(run_id)
    by_type = _events_by_type(run)
    assert store.lineage_edges(by_type[LCEventType.LLM_ENDED].id) == []


# ── Options ─────────────────────────────────────────────────────────────────────


def test_capture_payloads_off_omits_content():
    store = InMemoryTraceStore()
    tracer = DProvenanceTracer(store)
    with tracer.trace(context_id="c", capture_payloads=False) as cb:
        rid = uuid.uuid4()
        cb.on_llm_start({"id": ["ChatOpenAI"]}, ["secret prompt"], run_id=rid, parent_run_id=None)
        cb.on_llm_end(_FakeLLMResult(text="secret answer"), run_id=rid, parent_run_id=None)
        run_id = cb.run_id

    run = store.get_run(run_id)
    by_type = _events_by_type(run)
    assert "prompts" not in by_type[LCEventType.LLM_STARTED].payload.attributes
    assert "completion_preview" not in by_type[LCEventType.LLM_ENDED].payload.attributes
    # Structural metadata is still recorded.
    assert by_type[LCEventType.LLM_STARTED].payload.attributes["prompt_count"] == 1
    assert by_type[LCEventType.LLM_ENDED].payload.attributes["total_tokens"] == 12


def test_record_chains_off_skips_chain_events():
    store = InMemoryTraceStore()
    tracer = DProvenanceTracer(store)
    with tracer.trace(context_id="c", record_chains=False) as cb:
        _drive_rag_run(cb)
        run_id = cb.run_id

    run = store.get_run(run_id)
    types = {e.payload.type_identifier for e in run.events}
    assert LCEventType.CHAIN_STARTED not in types
    assert LCEventType.CHAIN_ENDED not in types
    assert LCEventType.LLM_STARTED in types


# ── Errors and agents ───────────────────────────────────────────────────────────


def test_tool_error_is_critical_with_diagnostics():
    store = InMemoryTraceStore()
    tracer = DProvenanceTracer(store)
    with tracer.trace(context_id="c") as cb:
        rid = uuid.uuid4()
        cb.on_tool_start({"name": "search"}, "q", run_id=rid, parent_run_id=None)
        cb.on_tool_error(ValueError("boom"), run_id=rid, parent_run_id=None)
        run_id = cb.run_id

    run = store.get_run(run_id)
    by_type = _events_by_type(run)
    err = by_type[LCEventType.TOOL_ERROR]
    assert err.payload.priority is TracePriority.CRITICAL
    assert err.payload.attributes == {"error_type": "ValueError", "message": "boom"}


def test_agent_finish_is_critical():
    store = InMemoryTraceStore()
    tracer = DProvenanceTracer(store)
    with tracer.trace(context_id="c") as cb:
        rid = uuid.uuid4()
        cb.on_agent_action(_FakeAgentAction("search", "kittens"), run_id=rid, parent_run_id=None)
        cb.on_agent_finish(_FakeAgentFinish("final answer"), run_id=rid, parent_run_id=None)
        run_id = cb.run_id

    run = store.get_run(run_id)
    by_type = _events_by_type(run)
    assert by_type[LCEventType.AGENT_ACTION].payload.attributes["tool"] == "search"
    finish = by_type[LCEventType.AGENT_FINISH]
    assert finish.payload.priority is TracePriority.CRITICAL
    assert finish.payload.attributes["output"] == "final answer"


# ── Querying recorded runs ──────────────────────────────────────────────────────


def test_recorded_run_is_queryable():
    store = InMemoryTraceStore()
    tracer = DProvenanceTracer(store)
    with tracer.trace(context_id="rag") as cb:
        _drive_rag_run(cb)

    # The README-style query: a run that consulted a retriever and reached an LLM.
    dsl = (
        TraceQueryDSL()
        .requiring_step(LCEventType.RETRIEVER_STARTED)
        .requiring_step(LCEventType.LLM_ENDED)
    )
    matches = store.query_runs(dsl)
    assert [r.context_id for r in matches] == ["rag"]

    # A run that errored on a tool — there are none here.
    assert store.query_runs(TraceQueryDSL().requiring_step(LCEventType.TOOL_ERROR)) == []


# ── Fingerprint: structural identity of an agent's execution path ───────────────


def _fingerprint_after(store: SQLiteTraceStore, run_id: uuid.UUID) -> str:
    store.flush()
    rows = store._db.query("SELECT fingerprint FROM runs WHERE run_id = ?", (str(run_id),))
    return rows[0][0]


def test_identical_paths_share_a_fingerprint_divergent_paths_do_not():
    with tempfile.TemporaryDirectory() as tmp:
        store = SQLiteTraceStore(LangChainTraceEvent, f"{tmp}/t.sqlite", start_writer=False)
        tracer = DProvenanceTracer(store)

        # Two runs that take the same structural path (retriever then llm).
        with tracer.trace(context_id="a") as cb:
            _drive_rag_run(cb)
            fa = cb.run_id
        with tracer.trace(context_id="b") as cb:
            _drive_rag_run(cb)
            fb = cb.run_id

        # A run that diverges: llm before retriever.
        with tracer.trace(context_id="c") as cb:
            root, retr, llm = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
            cb.on_chain_start({"name": "RagChain"}, {"question": "q"}, run_id=root, parent_run_id=None)
            cb.on_llm_start({"id": ["ChatOpenAI"]}, ["q"], run_id=llm, parent_run_id=root)
            cb.on_llm_end(_FakeLLMResult(), run_id=llm, parent_run_id=root)
            cb.on_retriever_start({"id": ["MyRetriever"]}, "q", run_id=retr, parent_run_id=root)
            cb.on_retriever_end([object()], run_id=retr, parent_run_id=root)
            cb.on_chain_end({"answer": "x"}, run_id=root, parent_run_id=None)
            fc = cb.run_id

        store.flush()
        fp_a = _fingerprint_after(store, fa)
        fp_b = _fingerprint_after(store, fb)
        fp_c = _fingerprint_after(store, fc)

    assert fp_a == fp_b  # same path → same fingerprint
    assert fp_a != fp_c  # reordered path → different fingerprint


# ── Real LangChain, when installed ──────────────────────────────────────────────


def test_real_langchain_runnable_records_a_run():
    pytest.importorskip("langchain_core")
    from langchain_core.runnables import RunnableLambda

    store = InMemoryTraceStore()
    tracer = DProvenanceTracer(store)
    chain = RunnableLambda(lambda x: x + 1) | RunnableLambda(lambda x: x * 2)

    with tracer.trace(context_id="runnable") as cb:
        result = chain.invoke(3, config={"callbacks": [cb]})
        run_id = cb.run_id

    assert result == 8
    run = store.get_run(run_id)
    assert run is not None
    types = {e.payload.type_identifier for e in run.events}
    assert LCEventType.CHAIN_STARTED in types
    assert LCEventType.CHAIN_ENDED in types

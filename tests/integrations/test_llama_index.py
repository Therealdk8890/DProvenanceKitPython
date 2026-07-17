"""Tests for the LlamaIndex integration.

LlamaIndex drives a ``BaseCallbackHandler`` by calling ``on_event_start`` /
``on_event_end`` with a ``CBEventType`` and an ``event_id`` / ``parent_id`` pair it
assigns at emission. We exercise the handler the same way — with a small stand-in for
``CBEventType`` — so the full translation is verified without installing
``llama-index-core`` (these tests run on every PR). The final section feeds *real*
``CBEventType`` values and a real ``CallbackManager`` through the handler when
LlamaIndex is installed (the scheduled integration job).
"""

from __future__ import annotations

import threading

import pytest

from dprovenancekit import TracePriority
from dprovenancekit.context import TraceContext
from dprovenancekit.edge import TraceEdgeType
from dprovenancekit.integrations.llama_index import (
    DProvenanceLlamaIndexCallbackHandler,
    LlamaIndexTraceEvent,
)
from dprovenancekit.kit import DProvenanceKit
from dprovenancekit.store import InMemoryTraceStore


# ── Stand-in for llama_index's CBEventType ──────────────────────────────────────


class FakeEventType:
    """Mimics a ``CBEventType`` member: an object with a string ``.value``."""

    def __init__(self, value: str):
        self.value = value


QUERY = FakeEventType("query")
RETRIEVE = FakeEventType("retrieve")
LLM = FakeEventType("llm")


def _recorded(store, run):
    return store.get_run(run.run_id).events


def _run_handler(drive, **handler_kwargs):
    """Open a run, drive the handler with ``drive(handler)``, return (store, run)."""
    kit = DProvenanceKit(LlamaIndexTraceEvent)
    store = InMemoryTraceStore()
    with kit.run("qa-session", store=store) as run:
        handler = DProvenanceLlamaIndexCallbackHandler(run, **handler_kwargs)
        drive(handler)
    return store, run


def _drive_nested_query(handler):
    """One query with one nested LLM call, ids/parents as LlamaIndex would assign."""
    handler.on_event_start(
        QUERY,
        payload={"query_str": "What did the author do growing up?"},
        event_id="ev-query",
        parent_id="root",
    )
    handler.on_event_start(
        LLM,
        payload={"serialized": {"model": "gpt-4o"}},
        event_id="ev-llm",
        parent_id="ev-query",
    )
    handler.on_event_end(LLM, payload={"response": "R" * 600}, event_id="ev-llm")
    handler.on_event_end(QUERY, payload={}, event_id="ev-query")


# ── Lineage: parent_id is the source of truth ───────────────────────────────────


def test_records_nested_events_via_parent_id():
    store, run = _run_handler(_drive_nested_query)

    events = _recorded(store, run)
    assert [e.payload.type_identifier for e in events] == [
        "queryStarted",
        "llmStarted",
        "llmEnded",
        "queryEnded",
    ]
    query_start, llm_start, llm_end, query_end = events

    # The LLM span nests under the query span; start/end share one span.
    assert query_start.span_id is not None
    assert query_start.parent_span_id is None  # "root" is LlamaIndex's top marker
    assert llm_start.parent_span_id == query_start.span_id
    assert llm_end.span_id == llm_start.span_id
    assert query_end.span_id == query_start.span_id

    attrs = query_start.payload.attributes
    assert attrs["query_str"] == "What did the author do growing up?"
    assert attrs["llama_event_id"] == "ev-query"


def test_concurrent_siblings_nest_under_their_real_parent():
    """Two retrievers open in parallel under one query: each must nest under the
    query, not under whichever sibling happened to start last (the stack bug)."""

    def drive(handler):
        handler.on_event_start(QUERY, event_id="q", parent_id="root")
        handler.on_event_start(RETRIEVE, event_id="r1", parent_id="q")
        handler.on_event_start(RETRIEVE, event_id="r2", parent_id="q")
        handler.on_event_end(RETRIEVE, event_id="r1")
        handler.on_event_end(RETRIEVE, event_id="r2")
        handler.on_event_end(QUERY, event_id="q")

    store, run = _run_handler(drive)
    by_id = {
        e.payload.attributes["llama_event_id"]: e
        for e in _recorded(store, run)
        if e.payload.type_identifier.endswith("Started")
    }
    assert by_id["r1"].parent_span_id == by_id["q"].span_id
    assert by_id["r2"].parent_span_id == by_id["q"].span_id  # not r1's span


def test_out_of_order_ends_keep_correct_spans():
    """A parent ending before a straggling child (async) must not corrupt lineage:
    the child's end still shares the child's span, parented under the query."""

    def drive(handler):
        handler.on_event_start(QUERY, event_id="q", parent_id="root")
        handler.on_event_start(LLM, event_id="l", parent_id="q")
        handler.on_event_end(QUERY, event_id="q")  # parent ends first
        handler.on_event_end(LLM, event_id="l")  # straggler

    store, run = _run_handler(drive)
    events = {
        (e.payload.attributes["llama_event_id"], e.payload.type_identifier): e
        for e in _recorded(store, run)
    }
    q_start = events[("q", "queryStarted")]
    l_start = events[("l", "llmStarted")]
    l_end = events[("l", "llmEnded")]
    assert events[("q", "queryEnded")].span_id == q_start.span_id
    assert l_end.span_id == l_start.span_id
    assert l_end.parent_span_id == q_start.span_id


def test_unknown_parent_falls_back_to_ambient_span():
    """An event whose parent this handler never saw nests under the ambient span, so
    a query fired inside instrumented code lands in the right place."""

    def drive(handler):
        token = TraceContext.current_span_id.set("ambient-span")
        try:
            handler.on_event_start(QUERY, event_id="q", parent_id="root")
            handler.on_event_end(QUERY, event_id="q")
        finally:
            TraceContext.current_span_id.reset(token)

    store, run = _run_handler(drive)
    q_start, q_end = _recorded(store, run)
    assert q_start.parent_span_id == "ambient-span"
    assert q_end.parent_span_id == "ambient-span"


def test_unmatched_end_is_tolerated():
    def drive(handler):
        handler.on_event_end(LLM, payload={}, event_id="never-started")

    store, run = _run_handler(drive)
    events = _recorded(store, run)
    assert [e.payload.type_identifier for e in events] == ["llmEnded"]
    assert store.impact_edges(events[0].id) == []


# ── Lifecycle edges ──────────────────────────────────────────────────────────────


def test_links_end_to_start():
    store, run = _run_handler(_drive_nested_query)
    query_start, llm_start, llm_end, query_end = _recorded(store, run)

    llm_edges = store.impact_edges(llm_start.id)
    assert [(e.source_id, e.target_id, e.type) for e in llm_edges] == [
        (llm_start.id, llm_end.id, TraceEdgeType.DERIVED_FROM)
    ]
    query_edges = store.impact_edges(query_start.id)
    assert [(e.source_id, e.target_id, e.type) for e in query_edges] == [
        (query_start.id, query_end.id, TraceEdgeType.DERIVED_FROM)
    ]


def test_link_lifecycle_off():
    store, run = _run_handler(_drive_nested_query, link_lifecycle=False)
    events = _recorded(store, run)
    assert len(events) == 4
    assert all(store.impact_edges(e.id) == [] for e in events)


# ── Engine identity ────────────────────────────────────────────────────────────────


def test_engine_derived_from_component():
    store, run = _run_handler(_drive_nested_query)
    query_start, llm_start, llm_end, query_end = _recorded(store, run)

    # The event type is the engine, unless a more specific component is known.
    assert query_start.engine_name == "query"
    assert query_end.engine_name == "query"
    # The LLM call's model (from EventPayload.SERIALIZED) is its engine, and the end
    # reuses the start's engine even though the end payload names no model.
    assert llm_start.engine_name == "gpt-4o"
    assert llm_end.engine_name == "gpt-4o"


def test_engine_from_tool_name():
    class Tool:
        name = "search"

    def drive(handler):
        handler.on_event_start(
            FakeEventType("function_call"),
            payload={"tool": Tool()},
            event_id="t",
            parent_id="root",
        )
        handler.on_event_end(FakeEventType("function_call"), event_id="t")

    store, run = _run_handler(drive)
    assert [e.engine_name for e in _recorded(store, run)] == ["search", "search"]


# ── Payload capture ────────────────────────────────────────────────────────────────


def test_payload_values_are_truncated():
    def drive(handler):
        handler.on_event_start(
            LLM, payload={"prompt": "P" * 5000}, event_id="l", parent_id="root"
        )
        handler.on_event_end(LLM, payload={"response": "R" * 5000}, event_id="l")

    store, run = _run_handler(drive)
    llm_start, llm_end = _recorded(store, run)
    assert llm_start.payload.attributes["prompt"] == "P" * 2000 + "…"
    assert llm_end.payload.attributes["response"] == "R" * 2000 + "…"


def test_serialized_config_secrets_are_redacted():
    """LlamaIndex's serialized LLM config has shipped an api_key in some versions. With
    capture on, secret-keyed values (including those nested in the serialized dict) must
    be redacted so the key never lands in a trace store shared as a golden baseline —
    while non-secret structure like the model name is preserved."""
    def drive(handler):
        handler.on_event_start(
            LLM,
            payload={
                "serialized": {"model": "gpt-4o", "api_key": "sk-SECRET123"},
                "api_key": "sk-TOPLEVEL",
                "total_tokens": 42,
            },
            event_id="l",
            parent_id="root",
        )
        handler.on_event_end(LLM, payload={}, event_id="l")

    store, run = _run_handler(drive)
    llm_start, _ = _recorded(store, run)
    attrs = llm_start.payload.attributes
    # No secret material anywhere in the recorded attributes.
    assert "sk-SECRET123" not in repr(attrs)
    assert "sk-TOPLEVEL" not in repr(attrs)
    assert attrs["api_key"] == "***redacted***"
    # Useful structure survives: model name kept, token counts not treated as secrets.
    assert "gpt-4o" in attrs["serialized"]
    assert "***redacted***" in attrs["serialized"]
    assert attrs["total_tokens"] == "42"


def test_node_lists_become_counts():
    def drive(handler):
        handler.on_event_start(RETRIEVE, event_id="r", parent_id="root")
        handler.on_event_end(
            RETRIEVE, payload={"nodes": ["n1", "n2", "n3"]}, event_id="r"
        )

    store, run = _run_handler(drive)
    _, retrieve_end = _recorded(store, run)
    attrs = retrieve_end.payload.attributes
    assert attrs["node_count"] == 3
    assert "nodes" not in attrs


def test_capture_payloads_off_keeps_only_structural_metadata():
    def drive(handler):
        handler.on_event_start(
            QUERY, payload={"query_str": "secret"}, event_id="q", parent_id="root"
        )
        handler.on_event_end(
            QUERY,
            payload={"response": "secret", "nodes": ["n1"]},
            event_id="q",
        )

    store, run = _run_handler(drive, capture_payloads=False)
    q_start, q_end = _recorded(store, run)
    assert q_start.payload.attributes == {"llama_event_id": "q"}
    assert q_end.payload.attributes == {"llama_event_id": "q", "node_count": 1}


# ── Errors ─────────────────────────────────────────────────────────────────────────


def test_exception_payload_records_critical_error_event():
    def drive(handler):
        handler.on_event_start(LLM, event_id="l", parent_id="root")
        handler.on_event_end(
            LLM,
            payload={"exception": ValueError("rate limited: " + "x" * 5000)},
            event_id="l",
        )

    store, run = _run_handler(drive)
    llm_start, llm_error = _recorded(store, run)

    assert llm_error.payload.type_identifier == "llmErrored"
    assert llm_error.payload.priority is TracePriority.CRITICAL
    attrs = llm_error.payload.attributes
    assert attrs["error_type"] == "ValueError"
    assert attrs["message"].startswith("rate limited: ")
    assert len(attrs["message"]) == 2001  # truncated + ellipsis

    # The error still closes the start's span and is linked to it.
    assert llm_error.span_id == llm_start.span_id
    assert [(e.source_id, e.target_id, e.type) for e in store.impact_edges(llm_start.id)] == [
        (llm_start.id, llm_error.id, TraceEdgeType.DERIVED_FROM)
    ]


# ── Thread safety ──────────────────────────────────────────────────────────────────


def test_handler_is_thread_safe():
    """Many threads driving one handler concurrently: every start/end pair must share
    a span and no event may be lost."""
    threads_n, per_thread = 8, 25

    def drive(handler):
        def worker(tid: int) -> None:
            for i in range(per_thread):
                event_id = f"{tid}-{i}"
                handler.on_event_start(LLM, event_id=event_id, parent_id="root")
                handler.on_event_end(LLM, event_id=event_id)

        threads = [
            threading.Thread(target=worker, args=(tid,)) for tid in range(threads_n)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    store, run = _run_handler(drive)
    events = _recorded(store, run)
    assert len(events) == threads_n * per_thread * 2

    spans_by_event: dict = {}
    for e in events:
        spans_by_event.setdefault(e.payload.attributes["llama_event_id"], set()).add(
            e.span_id
        )
    assert len(spans_by_event) == threads_n * per_thread
    assert all(len(spans) == 1 for spans in spans_by_event.values())


# ── Real LlamaIndex integration (scheduled job only; skips on PRs) ────────────────


def test_real_cbeventtype_and_event_payload_drive_the_handler():
    pytest.importorskip("llama_index.core")
    from llama_index.core.callbacks.base_handler import BaseCallbackHandler
    from llama_index.core.callbacks.schema import CBEventType, EventPayload

    kit = DProvenanceKit(LlamaIndexTraceEvent)
    store = InMemoryTraceStore()
    with kit.run("qa-session", store=store) as run:
        handler = DProvenanceLlamaIndexCallbackHandler(run)
        assert isinstance(handler, BaseCallbackHandler)
        handler.on_event_start(
            CBEventType.QUERY,
            payload={EventPayload.QUERY_STR: "q"},
            event_id="ev-q",
            parent_id="root",
        )
        handler.on_event_start(
            CBEventType.LLM,
            payload={EventPayload.SERIALIZED: {"model": "gpt-4o"}},
            event_id="ev-l",
            parent_id="ev-q",
        )
        handler.on_event_end(
            CBEventType.LLM, payload={EventPayload.RESPONSE: "r"}, event_id="ev-l"
        )
        handler.on_event_end(CBEventType.QUERY, payload={}, event_id="ev-q")

    events = store.get_run(run.run_id).events
    assert [e.payload.type_identifier for e in events] == [
        "queryStarted",
        "llmStarted",
        "llmEnded",
        "queryEnded",
    ]
    q_start, l_start, l_end, _ = events
    assert l_start.parent_span_id == q_start.span_id
    assert l_start.engine_name == "gpt-4o"
    # EventPayload str-enum keys land under their plain string names.
    assert q_start.payload.attributes["query_str"] == "q"
    assert l_end.payload.attributes["response"] == "r"


def test_real_callback_manager_supplies_correct_parent_ids():
    pytest.importorskip("llama_index.core")
    from llama_index.core.callbacks import CallbackManager
    from llama_index.core.callbacks.schema import CBEventType

    kit = DProvenanceKit(LlamaIndexTraceEvent)
    store = InMemoryTraceStore()
    with kit.run("qa-session", store=store) as run:
        handler = DProvenanceLlamaIndexCallbackHandler(run)
        manager = CallbackManager([handler])
        with manager.as_trace("query"):
            with manager.event(
                CBEventType.QUERY, payload={"query_str": "q"}
            ):
                with manager.event(CBEventType.RETRIEVE):
                    pass
                with manager.event(CBEventType.LLM):
                    pass

    started = {
        e.payload.type_identifier: e
        for e in store.get_run(run.run_id).events
        if e.payload.type_identifier.endswith("Started")
    }
    assert set(started) == {"queryStarted", "retrieveStarted", "llmStarted"}
    # The real CallbackManager's parent_id correlation drives the span tree: both the
    # retrieval and the LLM call nest under the query, as siblings.
    assert started["retrieveStarted"].parent_span_id == started["queryStarted"].span_id
    assert started["llmStarted"].parent_span_id == started["queryStarted"].span_id

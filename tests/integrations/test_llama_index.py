import pytest

from dprovenancekit.edge import TraceEdgeType
from dprovenancekit.kit import DProvenanceKit
from dprovenancekit.store import InMemoryTraceStore

pytest.importorskip("llama_index.core")

from llama_index.core.callbacks.schema import CBEventType

from dprovenancekit.integrations.llama_index import (
    DProvenanceLlamaIndexCallbackHandler,
    LlamaIndexTraceEvent,
)


def _run_nested_query(handler):
    """Drive the handler the way LlamaIndex's callback manager would for a
    query that makes one nested LLM call."""
    handler.on_event_start(
        CBEventType.QUERY,
        payload={"query_str": "What did the author do growing up?"},
        event_id="ev-query",
    )
    handler.on_event_start(
        CBEventType.LLM,
        payload={"prompt": "a very long prompt"},
        event_id="ev-llm",
        parent_id="ev-query",
    )
    handler.on_event_end(
        CBEventType.LLM,
        payload={"response": "R" * 600},
        event_id="ev-llm",
    )
    handler.on_event_end(CBEventType.QUERY, payload={}, event_id="ev-query")


def test_llama_index_handler_records_nested_events():
    kit = DProvenanceKit(LlamaIndexTraceEvent)
    store = InMemoryTraceStore()

    with kit.run("qa-session", store=store) as run:
        handler = DProvenanceLlamaIndexCallbackHandler(run)
        _run_nested_query(handler)

    run_snapshot = store.get_run(run.run_id)
    assert run_snapshot is not None
    events = run_snapshot.events
    assert [e.payload.type_identifier for e in events] == [
        "queryStarted",
        "llmStarted",
        "llmEnded",
        "queryEnded",
    ]
    assert all(e.engine_name == "llama_index" for e in events)

    query_start, llm_start, llm_end, query_end = events

    # The LLM span nests under the query span; start/end share one span.
    assert query_start.span_id is not None
    assert query_start.parent_span_id is None
    assert llm_start.parent_span_id == query_start.span_id
    assert llm_end.span_id == llm_start.span_id
    assert query_end.span_id == query_start.span_id

    # Queries are captured verbatim, prompts are dropped, responses previewed.
    attrs = query_start.payload.attributes
    assert attrs["query_str"] == "What did the author do growing up?"
    assert attrs["llama_event_id"] == "ev-query"
    assert "prompt" not in llm_start.payload.attributes
    preview = llm_end.payload.attributes["response_preview"]
    assert preview == "R" * 500 + "..."


def test_llama_index_handler_links_end_to_start():
    kit = DProvenanceKit(LlamaIndexTraceEvent)
    store = InMemoryTraceStore()

    with kit.run("qa-session", store=store) as run:
        handler = DProvenanceLlamaIndexCallbackHandler(run)
        _run_nested_query(handler)

    query_start, llm_start, llm_end, query_end = store.get_run(run.run_id).events

    llm_edges = store.impact_edges(llm_start.id)
    assert [(e.source_id, e.target_id, e.type) for e in llm_edges] == [
        (llm_start.id, llm_end.id, TraceEdgeType.DERIVED_FROM)
    ]
    query_edges = store.impact_edges(query_start.id)
    assert [(e.source_id, e.target_id, e.type) for e in query_edges] == [
        (query_start.id, query_end.id, TraceEdgeType.DERIVED_FROM)
    ]


def test_llama_index_handler_link_lifecycle_off():
    kit = DProvenanceKit(LlamaIndexTraceEvent)
    store = InMemoryTraceStore()

    with kit.run("qa-session", store=store) as run:
        handler = DProvenanceLlamaIndexCallbackHandler(run, link_lifecycle=False)
        _run_nested_query(handler)

    events = store.get_run(run.run_id).events
    assert len(events) == 4
    assert all(store.impact_edges(e.id) == [] for e in events)


def test_llama_index_handler_tolerates_unmatched_end():
    kit = DProvenanceKit(LlamaIndexTraceEvent)
    store = InMemoryTraceStore()

    with kit.run("qa-session", store=store) as run:
        handler = DProvenanceLlamaIndexCallbackHandler(run)
        handler.on_event_end(CBEventType.LLM, payload={}, event_id="never-started")

    events = store.get_run(run.run_id).events
    assert [e.payload.type_identifier for e in events] == ["llmEnded"]
    assert store.impact_edges(events[0].id) == []

"""The canonical vendor-neutral vocabulary and opt-in emission from live integrations."""

from __future__ import annotations

import json
import uuid
from importlib.resources import files

import pytest

from dprovenancekit import AnomalyDetector, InMemoryTraceStore, build_rules
from dprovenancekit import canonical as C
from dprovenancekit import otel_ingest as O
from dprovenancekit.canonical import (
    NATIVE_TYPE_ATTR,
    canonicalize_langchain,
    canonicalize_openai_agents,
)
from dprovenancekit.integrations.langchain import DProvenanceCallbackHandler
from dprovenancekit.integrations.openai_agents import DProvenanceTracingProcessor
from dprovenancekit.kit import ActiveTraceRun
from dprovenancekit.integrations.langchain import LangChainTraceEvent


# ── One vocabulary: canonical constants must match what OTel ingestion emits ──────


def test_canonical_kinds_match_otel_ingest():
    for kind in (
        "LLM_CALL",
        "TOOL_CALL",
        "AGENT_INVOCATION",
        "AGENT_CREATION",
        "CHAIN",
        "RETRIEVAL",
        "EMBEDDING",
        "RERANK",
        "GUARDRAIL",
    ):
        assert getattr(C, kind) == getattr(O, kind), kind


# ── Mapping functions ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "native,expected",
    [
        ("function.start", "tool_call.start"),
        ("function.end", "tool_call.end"),
        ("function.error", "tool_call.error"),
        ("generation.start", "llm_call.start"),
        ("response.end", "llm_call.end"),
        ("agent.start", "agent_invocation.start"),
        ("guardrail.end", "guardrail.end"),
    ],
)
def test_canonicalize_openai_agents(native, expected):
    assert canonicalize_openai_agents(native) == expected


def test_canonicalize_openai_agents_unknown_returns_none():
    assert canonicalize_openai_agents("mystery.start") is None
    assert canonicalize_openai_agents("noSuffix") is None


@pytest.mark.parametrize(
    "native,expected",
    [
        ("toolStarted", "tool_call.start"),
        ("toolEnded", "tool_call.end"),
        ("toolError", "tool_call.error"),
        ("llmStarted", "llm_call.start"),
        ("chatModelStarted", "llm_call.start"),
        ("retrieverEnded", "retrieval.end"),
        ("chainStarted", "chain.start"),
        ("agentAction", "agent_invocation.start"),
        ("agentFinish", "agent_invocation.end"),
    ],
)
def test_canonicalize_langchain(native, expected):
    assert canonicalize_langchain(native) == expected


def test_canonicalize_langchain_unknown_returns_none():
    assert canonicalize_langchain("text") is None


# ── openai-agents integration: fakes + driving ───────────────────────────────────


class FakeSpanData:
    def __init__(self, type_, **fields):
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


def _drive_openai_agents(proc, *, n_tools=1, trace_id="t1"):
    """agent → generation → n function calls, as the SDK would emit it."""
    proc.on_trace_start(FakeTrace(trace_id, "run"))
    agent = FakeSpan("s_agent", trace_id, FakeSpanData("agent", name="R", tools=["search"], handoffs=[]))
    proc.on_span_start(agent)
    gen = FakeSpan("s_gen", trace_id, FakeSpanData("generation", model="gpt-4o"), parent_id="s_agent")
    proc.on_span_start(gen)
    proc.on_span_end(gen)
    for i in range(n_tools):
        fn = FakeSpan(f"s_fn{i}", trace_id, FakeSpanData("function", name="search"), parent_id="s_agent")
        proc.on_span_start(fn)
        proc.on_span_end(fn)
    proc.on_span_end(agent)
    run_id = proc.run_id_for(trace_id)
    proc.on_trace_end(FakeTrace(trace_id, "run"))
    return run_id


def _types(store, run_id):
    return [e.payload.type_identifier for e in store.get_run(run_id).events]


def test_openai_agents_default_mode_keeps_native_names():
    store = InMemoryTraceStore()
    run_id = _drive_openai_agents(DProvenanceTracingProcessor(store))
    types = _types(store, run_id)
    assert "function.start" in types and "generation.start" in types
    assert not any(t.startswith("tool_call.") for t in types)  # backward compatible


def test_openai_agents_canonical_mode_emits_canonical_and_native_attr():
    store = InMemoryTraceStore()
    proc = DProvenanceTracingProcessor(store, canonical=True)
    run_id = _drive_openai_agents(proc)
    events = store.get_run(run_id).events
    types = [e.payload.type_identifier for e in events]
    assert "tool_call.start" in types and "tool_call.end" in types
    assert "llm_call.start" in types and "agent_invocation.start" in types
    assert not any(t.startswith("function.") for t in types)
    # native name preserved for traceability
    tool_start = next(e for e in events if e.payload.type_identifier == "tool_call.start")
    assert tool_start.payload.attributes[NATIVE_TYPE_ATTR] == "function.start"


# ── LangChain integration ────────────────────────────────────────────────────────


class _FakeLLMResult:
    def __init__(self, text="ok"):
        self.generations = [[type("G", (), {"text": text, "message": None})()]]
        self.llm_output = {}


def _drive_langchain(handler):
    """chain → tool → llm, as LangChain would emit it."""
    root, tool, llm = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    handler.on_chain_start({"name": "Agent"}, {"q": "x"}, run_id=root, parent_run_id=None)
    handler.on_tool_start({"name": "search"}, "q", run_id=tool, parent_run_id=root)
    handler.on_tool_end("3 results", run_id=tool, parent_run_id=root)
    handler.on_llm_start(
        {"id": ["langchain", "chat_models", "ChatOpenAI"]}, ["p"], run_id=llm, parent_run_id=root
    )
    handler.on_llm_end(_FakeLLMResult(), run_id=llm, parent_run_id=root)
    handler.on_chain_end({"answer": "a"}, run_id=root, parent_run_id=None)
    return root


def _langchain_run(canonical):
    store = InMemoryTraceStore()
    active = ActiveTraceRun(context_id="c", store=store, event_type=LangChainTraceEvent)
    handler = DProvenanceCallbackHandler(active, canonical=canonical)
    _drive_langchain(handler)
    active.flush()
    return store, active.run_id


def test_langchain_default_mode_keeps_native_names():
    store, run_id = _langchain_run(canonical=False)
    types = _types(store, run_id)
    assert "toolStarted" in types and "llmStarted" in types
    assert not any(t.startswith("tool_call.") for t in types)


def test_langchain_canonical_mode_emits_canonical():
    store, run_id = _langchain_run(canonical=True)
    events = store.get_run(run_id).events
    types = [e.payload.type_identifier for e in events]
    assert "tool_call.start" in types and "tool_call.end" in types
    assert "llm_call.start" in types
    assert not any(t.startswith("toolStarted") for t in types)
    tool = next(e for e in events if e.payload.type_identifier == "tool_call.start")
    assert tool.payload.attributes[NATIVE_TYPE_ATTR] == "toolStarted"


# ── The payoff: one vocabulary across integrations, and the bundled preset fires ──


def test_openai_agents_and_langchain_share_canonical_vocabulary():
    oa_store = InMemoryTraceStore()
    oa_run = _drive_openai_agents(DProvenanceTracingProcessor(oa_store, canonical=True))
    lc_store, lc_run = _langchain_run(canonical=True)

    oa_types = set(_types(oa_store, oa_run))
    lc_types = set(_types(lc_store, lc_run))
    # Both, recorded from different frameworks, speak the same tool/LLM vocabulary.
    shared = {"tool_call.start", "tool_call.end", "llm_call.start"}
    assert shared <= oa_types
    assert shared <= lc_types


def test_bundled_agent_preset_fires_on_canonical_openai_agents_run():
    store = InMemoryTraceStore()
    # 11 tool calls → runaway_tool_use (looping on tool_call.start > 10).
    run_id = _drive_openai_agents(
        DProvenanceTracingProcessor(store, canonical=True), n_tools=11
    )
    config = json.loads(
        files("dprovenancekit").joinpath("rulesets/agent.json").read_text(encoding="utf-8")
    )
    rules = build_rules(config["rules"])
    anomalies = AnomalyDetector(store).detect_anomalies(rules)
    assert any(a.rule_name == "agent.runaway_tool_use" and a.run_id == run_id for a in anomalies)

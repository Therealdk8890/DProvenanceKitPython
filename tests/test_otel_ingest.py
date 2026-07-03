"""OTel ingestion: OTLP JSON decoding, dialect classification, and end-to-end gating."""

from __future__ import annotations

import json
import uuid

import pytest

from dprovenancekit.diff import ChangeKind, TraceDiffEngine
from dprovenancekit.otel_ingest import (
    IngestedRun,
    OTelSpanEvent,
    ingest_otlp,
    run_id_for_trace,
)
from dprovenancekit.priority import TracePriority
from dprovenancekit.store import InMemoryTraceStore

TRACE_A = "0af7651916cd43dd8448eb211c80319c"
TRACE_B = "1bf7651916cd43dd8448eb211c80319d"


# ── OTLP JSON fixture builders ───────────────────────────────────────────────────


def _kv(key, value):
    if isinstance(value, bool):
        tagged = {"boolValue": value}
    elif isinstance(value, int):
        tagged = {"intValue": str(value)}  # int64 is a JSON string per OTLP spec
    elif isinstance(value, float):
        tagged = {"doubleValue": value}
    elif isinstance(value, list):
        tagged = {"arrayValue": {"values": [{"stringValue": str(v)} for v in value]}}
    else:
        tagged = {"stringValue": str(value)}
    return {"key": key, "value": tagged}


def _span(span_id, name, start, end, *, trace=TRACE_A, parent=None, attrs=None, error=False):
    span = {
        "traceId": trace,
        "spanId": span_id,
        "name": name,
        "kind": 1,
        "startTimeUnixNano": str(start),
        "endTimeUnixNano": str(end),
        "attributes": [_kv(k, v) for k, v in (attrs or {}).items()],
    }
    if parent is not None:
        span["parentSpanId"] = parent
    if error:
        span["status"] = {"code": 2, "message": "boom"}
    return span


def _request(spans, service="agent-svc"):
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": [_kv("service.name", service)]},
                "scopeSpans": [{"scope": {"name": "test"}, "spans": spans}],
            }
        ]
    }


def _agent_run(trace=TRACE_A, *, tools=("search", "verify"), loop_search=0):
    """An official-semconv agent run: invoke_agent → chat → execute_tool per tool."""
    spans = [
        _span(
            "a000000000000001",
            "invoke_agent Researcher",
            1_000,
            9_000,
            trace=trace,
            attrs={
                "gen_ai.operation.name": "invoke_agent",
                "gen_ai.agent.name": "Researcher",
            },
        ),
        _span(
            "a000000000000002",
            "chat gpt-5",
            1_500,
            2_500,
            trace=trace,
            parent="a000000000000001",
            attrs={
                "gen_ai.operation.name": "chat",
                "gen_ai.request.model": "gpt-5",
                "gen_ai.usage.input_tokens": 100,
                "gen_ai.usage.output_tokens": 20,
                "gen_ai.response.finish_reasons": ["tool_calls"],
            },
        ),
    ]
    t = 3_000
    seq = 3
    for tool in tools:
        spans.append(
            _span(
                f"a00000000000000{seq}",
                f"execute_tool {tool}",
                t,
                t + 500,
                trace=trace,
                parent="a000000000000001",
                attrs={
                    "gen_ai.operation.name": "execute_tool",
                    "gen_ai.tool.name": tool,
                },
            )
        )
        t += 1_000
        seq += 1
    for i in range(loop_search):
        spans.append(
            _span(
                f"a0000000000000{seq}{i}"[:16].ljust(16, "0"),
                "execute_tool search",
                t,
                t + 500,
                trace=trace,
                parent="a000000000000001",
                attrs={
                    "gen_ai.operation.name": "execute_tool",
                    "gen_ai.tool.name": "search",
                },
            )
        )
        t += 1_000
        seq += 1
    return _request(spans)


def _signatures(store, run_id):
    run = store.get_run(run_id)
    assert run is not None
    return [f"{e.payload.type_identifier}::{e.engine_name}" for e in run.events]


# ── Decoding and parsing shapes ──────────────────────────────────────────────────


def test_ingests_single_request_object():
    store = InMemoryTraceStore()
    runs = ingest_otlp(_agent_run(), store)
    assert len(runs) == 1
    assert runs[0].run_id == run_id_for_trace(TRACE_A)
    assert runs[0].context_id == "invoke_agent Researcher"
    assert runs[0].event_count == 8  # 4 spans × start/end


def test_ingests_json_lines_and_arrays(tmp_path):
    golden, candidate = _agent_run(TRACE_A), _agent_run(TRACE_B, tools=("search",))

    jsonl = tmp_path / "traces.jsonl"
    jsonl.write_text(json.dumps(golden) + "\n\n" + json.dumps(candidate) + "\n")
    store = InMemoryTraceStore()
    assert len(ingest_otlp(str(jsonl), store)) == 2

    array_file = tmp_path / "traces.json"
    array_file.write_text(json.dumps([golden, candidate]))
    store2 = InMemoryTraceStore()
    assert len(ingest_otlp(str(array_file), store2)) == 2


def test_invalid_input_raises_value_error(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json at all")
    with pytest.raises(ValueError):
        ingest_otlp(str(bad), InMemoryTraceStore())
    with pytest.raises(ValueError):
        ingest_otlp(str(tmp_path / "missing.json"), InMemoryTraceStore())


def test_decodes_tagged_values_and_snake_case():
    # snake_case field names (non-compliant but seen in the wild) still decode.
    span = {
        "trace_id": TRACE_A,
        "span_id": "b000000000000001",
        "name": "chat gpt-5",
        "start_time_unix_nano": 1000,  # number, not string — receivers must accept both
        "end_time_unix_nano": 2000,
        "attributes": [
            _kv("gen_ai.operation.name", "chat"),
            _kv("gen_ai.request.model", "gpt-5"),
            _kv("gen_ai.usage.input_tokens", 42),
        ],
    }
    store = InMemoryTraceStore()
    runs = ingest_otlp({"resource_spans": [{"scope_spans": [{"spans": [span]}]}]}, store)
    assert len(runs) == 1
    run = store.get_run(runs[0].run_id)
    end_event = run.events[-1]
    assert end_event.payload.type_identifier == "llm_call.end"
    assert end_event.payload.attributes["input_tokens"] == 42  # intValue string → int


# ── Structure: ordering, lineage, timestamps, determinism ───────────────────────


def test_depth_first_order_and_span_lineage():
    store = InMemoryTraceStore()
    (run,) = ingest_otlp(_agent_run(), store)
    assert _signatures(store, run.run_id) == [
        "agent_invocation.start::Researcher",
        "llm_call.start::gpt-5",
        "llm_call.end::gpt-5",
        "tool_call.start::search",
        "tool_call.end::search",
        "tool_call.start::verify",
        "tool_call.end::verify",
        "agent_invocation.end::Researcher",
    ]
    events = store.get_run(run.run_id).events
    assert [e.sequence for e in events] == list(range(8))
    root, llm = events[0], events[1]
    assert root.parent_span_id is None
    assert llm.parent_span_id == root.span_id
    # Historical OTLP times survive: 1_000 ns → 1e-6 seconds.
    assert events[0].timestamp == pytest.approx(1_000 / 1e9)


def test_run_id_is_deterministic_and_skippable():
    assert run_id_for_trace(TRACE_A) == uuid.UUID("0af76519-16cd-43dd-8448-eb211c80319c")
    store = InMemoryTraceStore()
    skipped = ingest_otlp(
        _agent_run(), store, skip_run_ids=frozenset({run_id_for_trace(TRACE_A)})
    )
    assert skipped == []


def test_orphan_span_becomes_root():
    span = _span(
        "c000000000000001",
        "execute_tool search",
        1000,
        2000,
        parent="dead000000000001",  # parent id never appears in the file
        attrs={"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "search"},
    )
    store = InMemoryTraceStore()
    (run,) = ingest_otlp(_request([span]), store)
    assert _signatures(store, run.run_id) == [
        "tool_call.start::search",
        "tool_call.end::search",
    ]


# ── Classification across dialects ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "attrs,name,expected_type,expected_engine",
    [
        # Official semconv, current generation
        (
            {"gen_ai.operation.name": "chat", "gen_ai.request.model": "gpt-5"},
            "chat gpt-5",
            "llm_call",
            "gpt-5",
        ),
        (
            {"gen_ai.operation.name": "retrieval", "gen_ai.data_source.id": "kb-1"},
            "retrieval kb-1",
            "retrieval",
            "kb-1",
        ),
        (
            {"gen_ai.operation.name": "invoke_workflow", "gen_ai.workflow.name": "rag"},
            "invoke_workflow rag",
            "chain",
            "rag",
        ),
        # Deprecated spellings still classify (gen_ai.system, prompt_tokens)
        (
            {"gen_ai.system": "openai", "gen_ai.usage.prompt_tokens": 10},
            "openai.chat",
            "llm_call",
            "openai",
        ),
        # OpenInference
        (
            {"openinference.span.kind": "TOOL", "tool.name": "calculator"},
            "calculator",
            "tool_call",
            "calculator",
        ),
        (
            {"openinference.span.kind": "LLM", "llm.model_name": "claude-sonnet-4-5"},
            "ChatAnthropic",
            "llm_call",
            "claude-sonnet-4-5",
        ),
        (
            {"openinference.span.kind": "AGENT"},
            "Planner",
            "agent_invocation",
            "Planner",
        ),
        # Unknown OpenInference kind → generic step, not a drop
        (
            {"openinference.span.kind": "PROMPT"},
            "template",
            "chain",
            "template",
        ),
        # Legacy OpenLLMetry / Traceloop
        (
            {"traceloop.span.kind": "tool", "traceloop.entity.name": "search"},
            "search.tool",
            "tool_call",
            "search",
        ),
        (
            {"llm.request.type": "chat", "gen_ai.request.model": "gpt-4o"},
            "openai.chat",
            "llm_call",
            "gpt-4o",
        ),
        # Vercel AI SDK
        (
            {"ai.operationId": "ai.toolCall", "ai.toolCall.name": "weather"},
            "ai.toolCall",
            "tool_call",
            "weather",
        ),
        (
            {"ai.operationId": "ai.generateText.doGenerate", "ai.model.id": "gpt-4o"},
            "ai.generateText.doGenerate",
            "llm_call",
            "gpt-4o",
        ),
        # Attribute-free span with a SHOULD-level name pattern still classifies
        ({}, "execute_tool lookup", "tool_call", "lookup"),
    ],
)
def test_dialect_classification(attrs, name, expected_type, expected_engine):
    store = InMemoryTraceStore()
    (run,) = ingest_otlp(_request([_span("d000000000000001", name, 1, 2, attrs=attrs)]), store)
    assert _signatures(store, run.run_id) == [
        f"{expected_type}.start::{expected_engine}",
        f"{expected_type}.end::{expected_engine}",
    ]


def test_unclassified_spans_dropped_by_default_kept_on_request():
    plain = _span("e000000000000001", "GET /api", 1, 2, attrs={"http.method": "GET"})
    store = InMemoryTraceStore()
    assert ingest_otlp(_request([plain]), store) == []  # nothing GenAI → no run

    store2 = InMemoryTraceStore()
    (run,) = ingest_otlp(_request([plain]), store2, include_unclassified=True)
    events = store2.get_run(run.run_id).events
    assert [e.payload.type_identifier for e in events] == ["span.start", "span.end"]
    assert all(e.payload.priority == TracePriority.TELEMETRY for e in events)


def test_error_span_emits_critical_error_event():
    failing = _span(
        "f000000000000001",
        "execute_tool search",
        1,
        2,
        attrs={"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "search"},
        error=True,
    )
    store = InMemoryTraceStore()
    (run,) = ingest_otlp(_request([failing]), store)
    end = store.get_run(run.run_id).events[-1]
    assert end.payload.type_identifier == "tool_call.error"
    assert end.payload.priority == TracePriority.CRITICAL
    assert end.payload.attributes["message"] == "boom"


def test_payload_previews_and_legacy_indexed_messages():
    llm = _span(
        "a100000000000001",
        "openai.chat",
        1,
        2,
        attrs={
            "llm.request.type": "chat",
            "gen_ai.request.model": "gpt-4o",
            "gen_ai.prompt.0.role": "user",
            "gen_ai.prompt.0.content": "What is 2+2?",
            "gen_ai.completion.0.content": "4",
        },
    )
    store = InMemoryTraceStore()
    (run,) = ingest_otlp(_request([llm]), store)
    events = store.get_run(run.run_id).events
    assert events[0].payload.attributes["input"] == "What is 2+2?"
    assert events[1].payload.attributes["output"] == "4"

    store2 = InMemoryTraceStore()
    (run2,) = ingest_otlp(_request([llm]), store2, capture_payloads=False)
    for event in store2.get_run(run2.run_id).events:
        assert "input" not in event.payload.attributes
        assert "output" not in event.payload.attributes


def test_context_id_falls_back_to_service_name():
    nameless = _span(
        "a200000000000001",
        "",
        1,
        2,
        attrs={"gen_ai.operation.name": "chat", "gen_ai.request.model": "gpt-5"},
    )
    store = InMemoryTraceStore()
    (run,) = ingest_otlp(_request([nameless], service="my-agent"), store)
    assert run.context_id == "my-agent"

    store2 = InMemoryTraceStore()
    (run2,) = ingest_otlp(_request([nameless]), store2, context_id="override")
    assert run2.context_id == "override"


# ── The point of it all: diff and gate ingested runs ─────────────────────────────


def test_diff_detects_dropped_tool_across_ingested_runs():
    store = InMemoryTraceStore()
    (golden,) = ingest_otlp(_agent_run(TRACE_A, tools=("search", "verify")), store)
    (candidate,) = ingest_otlp(_agent_run(TRACE_B, tools=("search",)), store)

    result = TraceDiffEngine().diff(
        store.get_run(golden.run_id), store.get_run(candidate.run_id)
    )
    removed = {
        (c.type_identifier, c.engine_name)
        for c in result.changes
        if c.kind == ChangeKind.REMOVED
    }
    assert ("tool_call.start", "verify") in removed
    assert ("tool_call.end", "verify") in removed


def test_cli_ingest_then_gate_fails_on_regression(tmp_path, capsys):
    from dprovenancekit.cli import main

    golden_file = tmp_path / "golden.json"
    golden_file.write_text(json.dumps(_agent_run(TRACE_A, tools=("search", "verify"))))
    candidate_file = tmp_path / "candidate.json"
    candidate_file.write_text(json.dumps(_agent_run(TRACE_B, tools=("search",))))
    db = str(tmp_path / "traces.sqlite")

    assert main(["ingest", str(golden_file), str(candidate_file), "--db", db]) == 0
    out = capsys.readouterr().out
    golden_id, candidate_id = str(run_id_for_trace(TRACE_A)), str(run_id_for_trace(TRACE_B))
    assert golden_id in out and candidate_id in out

    # Re-ingesting the same files is a no-op (deterministic run ids) → exit 1.
    assert main(["ingest", str(golden_file), "--db", db]) == 1
    capsys.readouterr()

    # The dropped `verify` tool fails the gate; identical runs pass it.
    assert (
        main(["gate", "--db", db, "--golden", golden_id, "--candidate", candidate_id])
        != 0
    )
    capsys.readouterr()
    assert (
        main(["gate", "--db", db, "--golden", golden_id, "--candidate", golden_id]) == 0
    )
    capsys.readouterr()


def test_cli_ingest_reports_nothing_found(tmp_path, capsys):
    from dprovenancekit.cli import main

    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps(_request([_span("ab00000000000001", "GET /", 1, 2)])))
    assert main(["ingest", str(empty), "--db", str(tmp_path / "t.sqlite")]) == 1
    assert "nothing ingested" in capsys.readouterr().err


def test_deeply_nested_trace_does_not_recurse(tmp_path):
    # A long chain of nested spans must not exhaust the Python recursion limit.
    spans = []
    for i in range(2000):
        span = _span(
            f"{i:016x}",
            f"execute_tool t{i}",
            1000 + i,
            9000 - i,
            parent=f"{i - 1:016x}" if i else None,
            attrs={"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": f"t{i}"},
        )
        spans.append(span)
    store = InMemoryTraceStore()
    (run,) = ingest_otlp(_request(spans), store)
    assert run.event_count == 4000  # 2000 spans × start/end


def test_within_command_duplicate_files_ingest_once(tmp_path):
    from dprovenancekit.cli import main

    f = tmp_path / "run.json"
    f.write_text(json.dumps(_agent_run(TRACE_A)))
    db = str(tmp_path / "t.sqlite")
    # The same file listed twice must not double-write the run.
    assert main(["ingest", str(f), str(f), "--db", db]) == 0

    import sqlite3

    conn = sqlite3.connect(db)
    (count,) = conn.execute(
        "SELECT event_count FROM runs WHERE run_id = ?",
        (str(run_id_for_trace(TRACE_A)),),
    ).fetchone()
    (rows,) = conn.execute(
        "SELECT COUNT(*) FROM trace_events WHERE run_id = ?",
        (str(run_id_for_trace(TRACE_A)),),
    ).fetchone()
    conn.close()
    assert count == 8 and rows == 8  # not 16


def test_base64_trace_id_matches_hex_form():
    import base64

    hex_id = "0af7651916cd43dd8448eb211c80319c"
    b64_id = base64.b64encode(bytes.fromhex(hex_id)).decode()
    span = _span(
        "aaaaaaaaaaaaaaa1",
        "execute_tool search",
        1,
        2,
        trace=b64_id,
        attrs={"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "search"},
    )
    store = InMemoryTraceStore()
    (run,) = ingest_otlp(_request([span]), store)
    # A base64 trace id (protobuf json_format) must not crash and must derive the same
    # run id as the hex encoding of the same bytes.
    assert run.run_id == run_id_for_trace(hex_id)


def test_odd_length_trace_id_does_not_crash():
    span = _span(
        "aaaaaaaaaaaaaaa1",
        "execute_tool search",
        1,
        2,
        trace="abc",  # 3 hex chars — zfill path
        attrs={"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "search"},
    )
    store = InMemoryTraceStore()
    (run,) = ingest_otlp(_request([span]), store)
    assert run.run_id == uuid.UUID("00000000-0000-0000-0000-000000000abc")


def test_duplicate_span_delivery_is_deduplicated():
    # OTLP allows at-least-once delivery; a re-sent span must not duplicate events.
    run = _agent_run(TRACE_A, tools=("search",))
    spans = run["resourceSpans"][0]["scopeSpans"][0]["spans"]
    dupped = _request(spans + spans)  # every span delivered twice
    store = InMemoryTraceStore()
    (ingested,) = ingest_otlp(dupped, store)
    clean = InMemoryTraceStore()
    (baseline,) = ingest_otlp(run, clean)
    assert ingested.event_count == baseline.event_count


def test_cli_ingest_rejects_non_sqlite_db(tmp_path, capsys):
    from dprovenancekit.cli import main

    junk = tmp_path / "notdb.txt"
    junk.write_text("this is not a database")
    src = tmp_path / "run.json"
    src.write_text(json.dumps(_agent_run(TRACE_A)))
    assert main(["ingest", str(src), "--db", str(junk)]) == 2
    assert "not a usable SQLite database" in capsys.readouterr().err


def test_rerank_and_guardrail_key_on_model_identity():
    rerank = _span(
        "aaaaaaaaaaaaaaa1",
        "CohereRerank",  # instrumentor span name — must NOT be the engine
        1,
        2,
        attrs={"openinference.span.kind": "RERANKER", "reranker.model_name": "rerank-v3"},
    )
    store = InMemoryTraceStore()
    (run,) = ingest_otlp(_request([rerank]), store)
    assert _signatures(store, run.run_id)[0] == "rerank.start::rerank-v3"


def test_event_payload_roundtrips_via_any_traceable_event():
    from dprovenancekit.event import AnyTraceableEvent

    payload = OTelSpanEvent.make(
        "tool_call.end", TracePriority.STRUCTURAL, {"tool": "search"}
    )
    decoded = AnyTraceableEvent.decode(payload.encode())
    assert decoded.type_identifier == "tool_call.end"
    assert decoded.priority == TracePriority.STRUCTURAL

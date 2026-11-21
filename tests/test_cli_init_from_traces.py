"""``dpk init --from-otel / --from-langsmith``: a golden baseline from existing traces."""

from __future__ import annotations

import json
import uuid

import pytest

from dprovenancekit.cli import main
from dprovenancekit.event import AnyTraceableEvent
from dprovenancekit.langsmith_import import (
    _to_nanos,
    langsmith_runs_to_otlp,
    parse_langsmith_runs,
)
from dprovenancekit.otel_ingest import ingest_otlp, run_id_for_trace
from dprovenancekit.sqlite_store import SQLiteTraceStore
from dprovenancekit.store import InMemoryTraceStore

TRACE_OLD = "0af7651916cd43dd8448eb211c80319c"
TRACE_NEW = "1bf7651916cd43dd8448eb211c80319d"
TRACE_REGRESSED = "2cf7651916cd43dd8448eb211c80319e"


def _kv(key, value):
    return {"key": key, "value": {"stringValue": value}}


def _otlp(trace_id, tools, start):
    spans = [
        {
            "traceId": trace_id,
            "spanId": "a" * 16,
            "name": "invoke_agent Support",
            "startTimeUnixNano": str(start),
            "endTimeUnixNano": str(start + 9000),
            "attributes": [
                _kv("gen_ai.operation.name", "invoke_agent"),
                _kv("gen_ai.agent.name", "Support"),
            ],
        }
    ]
    for i, tool in enumerate(tools):
        spans.append(
            {
                "traceId": trace_id,
                "spanId": f"{i + 1:016x}",
                "parentSpanId": "a" * 16,
                "name": f"execute_tool {tool}",
                "startTimeUnixNano": str(start + 1000 + i * 1000),
                "endTimeUnixNano": str(start + 1500 + i * 1000),
                "attributes": [
                    _kv("gen_ai.operation.name", "execute_tool"),
                    _kv("gen_ai.tool.name", tool),
                ],
            }
        )
    return {"resourceSpans": [{"scopeSpans": [{"spans": spans}]}]}


def _baseline_runs(project):
    store = SQLiteTraceStore(
        AnyTraceableEvent, str(project / ".dprovenance" / "baseline.sqlite"),
        start_writer=False,
    )
    try:
        return [row.run_id for row in store.list_run_metadata()]
    finally:
        store.close()


def test_from_otel_pins_newest_trace_then_gate_catches_a_dropped_tool(
    tmp_path, capsys, monkeypatch
):
    project = tmp_path / "project"
    history = tmp_path / "history.jsonl"
    history.write_text(
        json.dumps(_otlp(TRACE_OLD, ("lookup",), start=1_000)) + "\n"
        + json.dumps(_otlp(TRACE_NEW, ("lookup", "verify"), start=50_000)) + "\n"
    )
    assert main(["init", str(project), "--from-otel", str(history)]) == 0
    out = capsys.readouterr().out
    newest = str(run_id_for_trace(TRACE_NEW))
    assert f"* {newest}" in out and "Recorded baseline" in out
    assert _baseline_runs(project) == [newest]
    # No code scaffold in bootstrap mode.
    assert not (project / "agent_stub.py").exists()

    # A later trace that skips the verify tool fails the gate; following the printed
    # next steps verbatim.
    regressed = tmp_path / "regressed.json"
    regressed.write_text(
        json.dumps(_otlp(TRACE_REGRESSED, ("lookup",), start=90_000))
    )
    db = str(project / ".dprovenance" / "traces.sqlite")
    baseline = str(project / ".dprovenance" / "baseline.sqlite")
    assert main(["ingest", str(regressed), "--db", db]) == 0
    capsys.readouterr()
    assert main(["gate", "--baseline", baseline, "--candidate-db", db]) == 1
    assert "REGRESSION" in capsys.readouterr().out

    # Re-running init on the same history is idempotent and can pick another golden.
    assert (
        main(["init", str(project), "--from-otel", str(history),
              "--golden-trace", TRACE_OLD])
        == 0
    )
    capsys.readouterr()
    assert _baseline_runs(project) == [str(run_id_for_trace(TRACE_OLD))]


def test_from_otel_errors(tmp_path, capsys):
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"resourceSpans": []}))
    assert main(["init", str(tmp_path / "p"), "--from-otel", str(empty)]) == 1
    assert "no GenAI traces" in capsys.readouterr().err

    good = tmp_path / "good.json"
    good.write_text(json.dumps(_otlp(TRACE_OLD, ("lookup",), start=1)))
    assert (
        main(["init", str(tmp_path / "p"), "--from-otel", str(good),
              "--golden-trace", TRACE_NEW])
        == 2
    )
    assert "not among the imported traces" in capsys.readouterr().err

    assert main(["init", str(tmp_path / "p"), "--from-otel", str(tmp_path / "nope")]) == 2
    capsys.readouterr()
    assert main(["init", str(tmp_path / "p"), "--golden-trace", TRACE_OLD]) == 2
    assert "require --from-otel" in capsys.readouterr().err


# ── LangSmith ───────────────────────────────────────────────────────────────────

ROOT = "1f0c2a4e-8b3d-4c5e-9f6a-7b8c9d0e1f2a"
RETRIEVE = "1f0c2a4e-8b3d-4c5e-9f6a-7b8c9d0e1f2b"
LLM = "1f0c2a4e-8b3d-4c5e-9f6a-7b8c9d0e1f2c"
VERIFY = "1f0c2a4e-8b3d-4c5e-9f6a-7b8c9d0e1f2d"


def _langsmith_trace(*, with_verify=True, root=ROOT, suffix=""):
    def rid(base):
        return base if not suffix else str(uuid.uuid5(uuid.UUID(base), suffix))

    root_id = rid(root)
    runs = [
        {
            "id": root_id, "trace_id": root_id, "parent_run_id": None,
            "name": "support_agent", "run_type": "chain",
            "start_time": "2026-11-20T10:00:00.000000", "end_time": "2026-11-20T10:00:05Z",
            "inputs": {"question": "refund?"}, "outputs": {"answer": "approved"},
            "error": None, "extra": {"metadata": {}},
        },
        {
            "id": rid(RETRIEVE), "trace_id": root_id, "parent_run_id": root_id,
            "name": "lookup_order", "run_type": "tool",
            "start_time": "2026-11-20T10:00:01.000000",
            "end_time": "2026-11-20T10:00:01.500000",
            "inputs": {"order": 42}, "outputs": {"status": "shipped"},
        },
        {
            "id": rid(LLM), "trace_id": root_id, "parent_run_id": root_id,
            "name": "ChatOpenAI", "run_type": "llm",
            "start_time": "2026-11-20T10:00:02.000000+00:00",
            "end_time": "2026-11-20T10:00:03.000000+00:00",
            "extra": {"metadata": {"ls_model_name": "gpt-5", "ls_provider": "openai"}},
        },
    ]
    if with_verify:
        runs.append(
            {
                "id": rid(VERIFY), "trace_id": root_id, "parent_run_id": root_id,
                "name": "verify_refund_policy", "run_type": "tool",
                "start_time": "2026-11-20T10:00:04.000000",
                "end_time": "2026-11-20T10:00:04.200000",
            }
        )
    return runs


def test_langsmith_runs_map_to_openinference_spans():
    request = langsmith_runs_to_otlp(_langsmith_trace())
    store = InMemoryTraceStore()
    (run,) = ingest_otlp(request, store)
    # The DPK run id IS the LangSmith trace id.
    assert run.run_id == uuid.UUID(ROOT)
    assert run.context_id == "support_agent"
    events = store.get_run(run.run_id).events
    signatures = [(e.payload.type_identifier, e.engine_name) for e in events]
    assert signatures == [
        ("chain.start", "support_agent"),
        ("tool_call.start", "lookup_order"),
        ("tool_call.end", "lookup_order"),
        ("llm_call.start", "gpt-5"),
        ("llm_call.end", "gpt-5"),
        ("tool_call.start", "verify_refund_policy"),
        ("tool_call.end", "verify_refund_policy"),
        ("chain.end", "support_agent"),
    ]
    # Span ids are the LangSmith run ids, so a verdict can be routed back.
    assert events[1].span_id == uuid.UUID(RETRIEVE).hex


def test_langsmith_input_shapes_and_nested_child_runs():
    runs = _langsmith_trace()
    nested = dict(runs[0], child_runs=runs[1:])
    for text in (
        json.dumps(runs),
        "\n".join(json.dumps(r) for r in runs),
        json.dumps({"runs": runs}),
        json.dumps(nested),
    ):
        assert len(parse_langsmith_runs(text)) == 4
    with pytest.raises(ValueError):
        parse_langsmith_runs("{not json\n")


def test_langsmith_errors_and_missing_trace_ids():
    runs = _langsmith_trace(with_verify=False)
    for run in runs:
        run.pop("trace_id")  # inherited from the parent chain
    runs[1]["error"] = "Timeout"
    store = InMemoryTraceStore()
    (run,) = ingest_otlp(langsmith_runs_to_otlp(runs), store)
    assert run.run_id == uuid.UUID(ROOT)
    types = [e.payload.type_identifier for e in store.get_run(run.run_id).events]
    assert "tool_call.error" in types


def test_timestamp_parsing():
    assert _to_nanos("1970-01-01T00:00:01.5Z") == 1_500_000_000
    assert _to_nanos("1970-01-01T01:00:00+01:00") == 0
    assert _to_nanos("1970-01-01 00:00:00.000000001") == 1
    assert _to_nanos(2) == 2_000_000_000
    with pytest.raises(ValueError):
        _to_nanos("yesterday")


def test_init_from_langsmith_then_gate_detects_skipped_verify(tmp_path, capsys):
    project = tmp_path / "project"
    golden = tmp_path / "langsmith.json"
    golden.write_text(json.dumps(_langsmith_trace()))
    assert main(["init", str(project), "--from-langsmith", str(golden)]) == 0
    capsys.readouterr()
    assert _baseline_runs(project) == [ROOT]

    later = tmp_path / "later.json"
    later_runs = _langsmith_trace(with_verify=False, suffix="later")
    for run in later_runs:  # a later trace: shift start times forward a day
        for key in ("start_time", "end_time"):
            if run.get(key):
                run[key] = run[key].replace("2026-11-20", "2026-11-21")
    later.write_text(json.dumps(later_runs))
    db = str(project / ".dprovenance" / "traces.sqlite")
    assert main(["ingest", str(later), "--db", db, "--format", "langsmith"]) == 0
    capsys.readouterr()
    code = main(
        ["gate", "--baseline", str(project / ".dprovenance" / "baseline.sqlite"),
         "--candidate-db", db, "--json"]
    )
    report = json.loads(capsys.readouterr().out)
    assert code == 1 and report["passed"] is False
    assert report["golden_run_id"] == ROOT
    assert "tool_call.start" in report["steps_by_change"]["removed"]

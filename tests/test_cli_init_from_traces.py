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


def test_from_otel_merges_a_trace_split_across_files(tmp_path, capsys):
    whole = _otlp(TRACE_NEW, ("lookup", "verify"), start=1_000)
    spans = whole["resourceSpans"][0]["scopeSpans"][0]["spans"]
    first, second = tmp_path / "part1.json", tmp_path / "part2.json"
    first.write_text(json.dumps({"resourceSpans": [{"scopeSpans": [{"spans": spans[:2]}]}]}))
    second.write_text(json.dumps({"resourceSpans": [{"scopeSpans": [{"spans": spans[2:]}]}]}))
    # A '#' in the project path must not break the re-run idempotency check.
    project = tmp_path / "team #1"
    for _ in range(2):
        assert main(["init", str(project), "--from-otel", str(first), str(second)]) == 0
    out = capsys.readouterr().out
    assert "events=6" in out  # root start/end + 2 tools x start/end
    store = SQLiteTraceStore(
        AnyTraceableEvent, str(project / ".dprovenance" / "traces.sqlite"),
        start_writer=False,
    )
    try:
        events = store.get_run(run_id_for_trace(TRACE_NEW)).events
    finally:
        store.close()
    assert len(events) == 6  # not duplicated by the second run
    assert [e.engine_name for e in events if e.payload.type_identifier == "tool_call.start"] == [
        "lookup",
        "verify",
    ]


def test_gate_refuses_otlp_out_over_an_input_database(tmp_path, capsys):
    history = tmp_path / "t.json"
    history.write_text(json.dumps(_otlp(TRACE_OLD, ("lookup",), start=1)))
    project = tmp_path / "p"
    assert main(["init", str(project), "--from-otel", str(history)]) == 0
    db = project / ".dprovenance" / "traces.sqlite"
    baseline = project / ".dprovenance" / "baseline.sqlite"
    before = db.read_bytes()
    link = tmp_path / "alias.sqlite"
    link.symlink_to(db)
    capsys.readouterr()
    for target in (db, link):
        code = main(
            ["gate", "--baseline", str(baseline), "--candidate-db", str(db),
             "--otlp-out", str(target)]
        )
        assert code == 2
        assert "input trace databases" in capsys.readouterr().err
    assert db.read_bytes() == before
    out = tmp_path / "verdict.json"
    assert main(
        ["gate", "--baseline", str(baseline), "--candidate-db", str(db),
         "--otlp-out", str(out)]
    ) == 0
    assert json.loads(out.read_text())["resourceSpans"]
    assert not list(tmp_path.glob(".dpk-*.tmp"))


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
    # OTLP-legal 8-byte span ids; the full LangSmith run id stays an attribute.
    spans = request["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert all(len(span["spanId"]) == 16 for span in spans)
    assert all(len(span.get("parentSpanId", "0" * 16)) == 16 for span in spans)
    assert len({span["spanId"] for span in spans}) == len(spans)
    tool = next(span for span in spans if span["name"] == "lookup_order")
    assert {"key": "langsmith.run_id", "value": {"stringValue": RETRIEVE}} in tool[
        "attributes"
    ]
    assert tool["parentSpanId"] == next(
        span["spanId"] for span in spans if span["name"] == "support_agent"
    )
    assert events[1].span_id == tool["spanId"]


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


def test_nested_child_runs_inherit_their_enclosing_run():
    runs = _langsmith_trace()
    children = []
    for child in runs[1:]:
        child = dict(child)
        child.pop("parent_run_id")
        child.pop("trace_id")
        children.append(child)
    nested = dict(runs[0], child_runs=children)
    store = InMemoryTraceStore()
    (run,) = ingest_otlp(langsmith_runs_to_otlp(parse_langsmith_runs(json.dumps(nested))), store)
    assert run.run_id == uuid.UUID(ROOT) and run.event_count == 8


def test_unfinished_langsmith_traces_are_not_imported(tmp_path, capsys):
    from dprovenancekit.langsmith_import import unfinished_trace_ids

    runs = _langsmith_trace()
    runs[3]["end_time"] = None  # verify_refund_policy still running
    assert unfinished_trace_ids(runs) == [ROOT]
    assert ingest_otlp(langsmith_runs_to_otlp(runs), InMemoryTraceStore()) == []
    errored = [dict(r) for r in runs]
    errored[3]["error"] = "Timeout"  # errored runs are finished
    assert unfinished_trace_ids(errored) == []

    path = tmp_path / "running.json"
    path.write_text(json.dumps(runs))
    assert main(["init", str(tmp_path / "p"), "--from-langsmith", str(path)]) == 1
    captured = capsys.readouterr()
    assert "unfinished runs" in captured.err and "no GenAI traces" in captured.err


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
    assert "--format langsmith" in capsys.readouterr().out
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


# ── review follow-ups (#121) ───────────────────────────────────────────────────


def _split(request, at):
    spans = request["resourceSpans"][0]["scopeSpans"][0]["spans"]
    return [
        {"resourceSpans": [{"scopeSpans": [{"spans": part}]}]}
        for part in (spans[:at], spans[at:])
    ]


def _stored_events(db, run_id):
    store = SQLiteTraceStore(AnyTraceableEvent, str(db), start_writer=False)
    try:
        run = store.get_run(run_id)
        return [] if run is None else run.events
    finally:
        store.close()


def test_init_refuses_to_pin_a_stale_partial_copy(tmp_path, capsys):
    whole = _otlp(TRACE_NEW, ("lookup", "verify"), start=1_000)
    partial, rest = _split(whole, 2)  # agent + lookup | verify
    first, second = tmp_path / "part1.json", tmp_path / "part2.json"
    first.write_text(json.dumps(partial))
    second.write_text(json.dumps(rest))
    project = tmp_path / "p"
    db = project / ".dprovenance" / "traces.sqlite"
    baseline = project / ".dprovenance" / "baseline.sqlite"

    assert main(["init", str(project), "--from-otel", str(first)]) == 0
    assert "events=4" in capsys.readouterr().out
    db_before, baseline_before = db.read_bytes(), baseline.read_bytes()

    # The second fragment completes the trace: init must not pin the stale 4-event copy.
    assert main(["init", str(project), "--from-otel", str(first), str(second)]) == 2
    err = capsys.readouterr().err
    assert str(run_id_for_trace(TRACE_NEW)) in err and "fresh directory" in err
    assert db.read_bytes() == db_before and baseline.read_bytes() == baseline_before

    # Identical inputs stay idempotent, and a fresh project gets the complete trace.
    assert main(["init", str(project), "--from-otel", str(first)]) == 0
    fresh = tmp_path / "fresh"
    assert main(["init", str(fresh), "--from-otel", str(first), str(second)]) == 0
    assert "events=6" in capsys.readouterr().out
    pinned = _stored_events(
        fresh / ".dprovenance" / "baseline.sqlite", run_id_for_trace(TRACE_NEW)
    )
    assert len(pinned) == 6


@pytest.mark.parametrize("order", ["root_first", "child_first"])
def test_ingest_never_stores_a_langsmith_trace_with_an_unfinished_child_elsewhere(
    tmp_path, capsys, order
):
    runs = _langsmith_trace()
    root_only, children = runs[:1], [dict(r) for r in runs[1:]]
    children[-1]["end_time"] = None  # verify_refund_policy still running
    a, b = tmp_path / "root.json", tmp_path / "children.json"
    a.write_text(json.dumps(root_only))
    b.write_text(json.dumps(children))
    files = [str(a), str(b)] if order == "root_first" else [str(b), str(a)]
    db = tmp_path / "t.sqlite"

    assert main(["ingest", *files, "--db", str(db), "--format", "langsmith"]) == 1
    err = capsys.readouterr().err
    assert f"skipped LangSmith trace {ROOT}" in err
    assert _stored_events(db, uuid.UUID(ROOT)) == []

    # Once the run finishes, the complete export is accepted.
    done = tmp_path / "done.json"
    done.write_text(json.dumps(_langsmith_trace()))
    assert main(["ingest", str(done), "--db", str(db), "--format", "langsmith"]) == 0
    assert "events=8" in capsys.readouterr().out
    assert len(_stored_events(db, uuid.UUID(ROOT))) == 8


def test_ingest_warns_when_a_stored_trace_differs(tmp_path, capsys):
    whole = _otlp(TRACE_NEW, ("lookup", "verify"), start=1_000)
    partial, _ = _split(whole, 2)
    p, w = tmp_path / "partial.json", tmp_path / "whole.json"
    p.write_text(json.dumps(partial))
    w.write_text(json.dumps(whole))
    db = tmp_path / "t.sqlite"
    assert main(["ingest", str(p), "--db", str(db)]) == 0
    assert main(["ingest", str(p), "--db", str(db)]) == 1  # identical: no warning
    assert "different content" not in capsys.readouterr().err
    assert main(["ingest", str(w), "--db", str(db)]) == 1
    assert "different content" in capsys.readouterr().err


def _b64(hex_id):
    import base64

    return base64.b64encode(bytes.fromhex(hex_id)).decode()


def test_hex_and_base64_ids_of_one_trace_form_one_tree(tmp_path, capsys):
    whole = _otlp(TRACE_NEW, ("lookup",), start=1_000)
    collector, protobuf = _split(whole, 1)  # root (hex) | tool child (base64 ids)
    for span in protobuf["resourceSpans"][0]["scopeSpans"][0]["spans"]:
        span["traceId"] = _b64(span["traceId"])
        span["spanId"] = _b64(span["spanId"])
        span["parentSpanId"] = _b64(span["parentSpanId"])
    store = InMemoryTraceStore()
    (run,) = ingest_otlp([collector, protobuf], store)
    events = store.get_run(run.run_id).events
    assert run.run_id == run_id_for_trace(TRACE_NEW) and run.trace_id == TRACE_NEW
    assert [e.sequence for e in events] == [0, 1, 2, 3]
    assert [e.payload.type_identifier for e in events] == [
        "agent_invocation.start", "tool_call.start", "tool_call.end",
        "agent_invocation.end",
    ]
    tool = next(e for e in events if e.payload.type_identifier == "tool_call.start")
    assert tool.span_id == f"{1:016x}" and tool.parent_span_id == "a" * 16

    # Through the CLI, across two files, the pinned baseline is that single tree.
    f1, f2 = tmp_path / "collector.json", tmp_path / "protobuf.json"
    f1.write_text(json.dumps(collector))
    f2.write_text(json.dumps(protobuf))
    project = tmp_path / "p"
    assert main(["init", str(project), "--from-otel", str(f1), str(f2)]) == 0
    capsys.readouterr()
    pinned = _stored_events(
        project / ".dprovenance" / "baseline.sqlite", run_id_for_trace(TRACE_NEW)
    )
    assert sorted(e.sequence for e in pinned) == [0, 1, 2, 3]

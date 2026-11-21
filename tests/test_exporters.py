"""Tests for the gate-verdict exporters (``dprovenancekit.exporters``).

Vendor SDKs are never contacted: adapters are driven with recording fakes. Where a vendor
SDK happens to be installed, its real method signature is additionally checked so the
adapters can only ever send keyword arguments that the real API declares.
"""

from __future__ import annotations

import inspect
import json
import uuid
from dataclasses import dataclass

import pytest

from dprovenancekit import InMemoryTraceStore, RegressionGate, TraceableEvent, TracePriority
from dprovenancekit.cli import main
from dprovenancekit.exporters import (
    VERDICT_SCHEMA,
    GateVerdict,
    attach_verdict,
    record_verdict_span,
    verdict_to_otlp_json,
)
from dprovenancekit.exporters.datadog import send_verdict_to_datadog
from dprovenancekit.exporters.langfuse import send_verdict_to_langfuse
from dprovenancekit.exporters.langsmith import send_verdict_to_langsmith
from dprovenancekit.exporters.otel import VERDICT_EVENT_NAME
from dprovenancekit.exporters.phoenix import send_verdict_to_phoenix
from dprovenancekit.kit import DProvenanceKit
from dprovenancekit.otel_ingest import _decode_attributes, ingest_otlp, run_id_for_trace

TRACE_GOLDEN = "0af7651916cd43dd8448eb211c80319c"
TRACE_CANDIDATE = "4bf92f3577b34da6a3ce929d0e0e4736"


# ── fixtures ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Step(TraceableEvent):
    kind: str

    @property
    def type_identifier(self) -> str:
        return self.kind

    @property
    def priority(self) -> TracePriority:
        return TracePriority.CRITICAL if self.kind == "verify" else TracePriority.STRUCTURAL

    def to_dict(self) -> dict:
        return {"kind": self.kind}

    @classmethod
    def from_dict(cls, data: dict) -> "Step":
        return cls(kind=data["kind"])


def _run(store, kinds):
    kit = DProvenanceKit(Step)
    with kit.run(context_id="account-check", store=store) as run:
        for kind in kinds:
            kit.record(Step(kind))
    return store.get_run(run.run_id)


def _failing_report():
    store = InMemoryTraceStore()
    golden = _run(store, ["retrieve", "verify", "decide"])
    candidate = _run(store, ["retrieve", "decide"])
    return golden, candidate, RegressionGate().check(golden, candidate)


def _kv(key, value):
    return {"key": key, "value": {"stringValue": value}}


def _otlp(trace_id, tools):
    spans = [
        {
            "traceId": trace_id,
            "spanId": "a" * 16,
            "name": "invoke_agent Planner",
            "startTimeUnixNano": "1000",
            "endTimeUnixNano": "9000",
            "attributes": [
                _kv("gen_ai.operation.name", "invoke_agent"),
                _kv("gen_ai.agent.name", "Planner"),
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
                "startTimeUnixNano": str(2000 + i * 1000),
                "endTimeUnixNano": str(2500 + i * 1000),
                "attributes": [
                    _kv("gen_ai.operation.name", "execute_tool"),
                    _kv("gen_ai.tool.name", tool),
                ],
            }
        )
    return {"resourceSpans": [{"scopeSpans": [{"spans": spans}]}]}


class RecordingSpan:
    def __init__(self):
        self.attributes = {}
        self.events = []
        self.status = None

    def set_attribute(self, key, value):
        self.attributes[key] = value

    def add_event(self, name, attributes=None):
        self.events.append((name, dict(attributes or {})))

    def set_status(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class RecordingTracer:
    def __init__(self):
        self.spans = []

    def start_as_current_span(self, name, attributes=None):
        span = RecordingSpan()
        span.name = name
        span.attributes.update(attributes or {})
        self.spans.append(span)
        return span


# ── GateVerdict ─────────────────────────────────────────────────────────────────


def test_from_report_maps_a_removed_critical_step():
    golden, candidate, report = _failing_report()
    verdict = GateVerdict.from_report(
        report,
        golden_run_id=golden.run_id,
        candidate_run_id=candidate.run_id,
        profile="strict_audit_v1",
        evidence={"attestation.sha256": "ab" * 32},
    )
    attrs = verdict.to_attributes()
    assert attrs["dpk.schema"] == VERDICT_SCHEMA
    assert attrs["dpk.gate.passed"] is False and attrs["dpk.gate.verdict"] == "fail"
    assert attrs["dpk.gate.regression_level"] == "high"
    assert attrs["dpk.gate.steps.removed"] == ["verify"]
    assert attrs["dpk.golden.run_id"] == str(golden.run_id)
    assert attrs["dpk.candidate.run_id"] == str(candidate.run_id)
    assert attrs["dpk.evidence.attestation.sha256"] == "ab" * 32
    assert attrs["dpk.sdk.name"] == "dprovenancekit-python"
    assert "verify" in verdict.summary() and verdict.score == 0.0
    # Every value is an OTel-legal primitive or a homogeneous list of strings.
    for value in attrs.values():
        assert isinstance(value, (str, bool, int, float)) or (
            isinstance(value, list) and all(isinstance(v, str) for v in value)
        )
    json.dumps(verdict.to_dict())  # JSON-ready


def test_from_gate_json_requires_passed_and_rejects_bad_evidence_keys():
    with pytest.raises(ValueError):
        GateVerdict.from_gate_json({"regression_level": "none"})
    with pytest.raises(ValueError):
        GateVerdict(passed=True).from_gate_json({"passed": True}, evidence={"bad key": "x"})


# ── OpenTelemetry ───────────────────────────────────────────────────────────────


def test_attach_verdict_sets_attributes_and_event_without_touching_status():
    _, _, report = _failing_report()
    verdict = GateVerdict.from_report(report)
    span = RecordingSpan()
    attach_verdict(span, verdict)
    assert span.attributes["dpk.gate.verdict"] == "fail"
    assert span.events[0][0] == VERDICT_EVENT_NAME
    assert span.events[0][1] == verdict.to_attributes()
    name, standard = span.events[1]
    assert name == "gen_ai.evaluation.result"
    assert standard["gen_ai.evaluation.name"] == "dpk_gate"
    assert standard["gen_ai.evaluation.score.value"] == 0.0
    assert standard["gen_ai.evaluation.score.label"] == "fail"
    assert "verify" in standard["gen_ai.evaluation.explanation"]
    assert span.status is None

    plain = RecordingSpan()
    attach_verdict(plain, verdict, evaluation_event=False, set_attributes=False)
    assert [e[0] for e in plain.events] == [VERDICT_EVENT_NAME] and not plain.attributes


def test_record_verdict_span_with_fake_tracer():
    tracer = RecordingTracer()
    record_verdict_span(tracer, GateVerdict(passed=True))
    (span,) = tracer.spans
    assert span.name == "dpk.gate"
    assert span.attributes["dpk.gate.passed"] is True
    assert span.status is None  # passing gate: status untouched


def test_record_verdict_span_with_real_otel_sdk():
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )
    from opentelemetry.trace import StatusCode

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")
    _, _, report = _failing_report()
    with tracer.start_as_current_span("agent-ci") as parent:
        record_verdict_span(tracer, GateVerdict.from_report(report))
        attach_verdict(parent, GateVerdict.from_report(report))
    finished = {s.name: s for s in exporter.get_finished_spans()}
    gate = finished["dpk.gate"]
    assert gate.parent.span_id == finished["agent-ci"].context.span_id
    assert gate.attributes["dpk.gate.steps.removed"] == ("verify",)
    assert gate.status.status_code is StatusCode.ERROR
    assert [e.name for e in gate.events] == [VERDICT_EVENT_NAME, "gen_ai.evaluation.result"]
    assert finished["agent-ci"].attributes["dpk.gate.verdict"] == "fail"


def test_otlp_json_defaults_to_candidate_trace_and_round_trips_through_ingest():
    candidate_run = str(run_id_for_trace(TRACE_CANDIDATE))
    verdict = GateVerdict(
        passed=False, regression_level="high", candidate_run_id=candidate_run,
        removed_steps=("tool_call.start",), strength=1.0,
    )
    request = verdict_to_otlp_json(verdict, end_time_ns=5_000, span_id="b" * 16)
    span = request["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    assert span["traceId"] == TRACE_CANDIDATE
    assert span["status"]["code"] == 2 and "FAIL" in span["status"]["message"]
    assert _decode_attributes(span["attributes"]) == verdict.to_attributes()
    assert [e["name"] for e in span["events"]] == [VERDICT_EVENT_NAME, "gen_ai.evaluation.result"]
    assert _decode_attributes(span["events"][1]["attributes"]) == (
        verdict.evaluation_result_attributes()
    )

    # DPK's own OTLP reader accepts it (as an unclassified telemetry span).
    store = InMemoryTraceStore()
    runs = ingest_otlp(request, store, include_unclassified=True)
    assert [str(r.run_id) for r in runs] == [candidate_run]


def test_otlp_json_validates_ids():
    verdict = GateVerdict(passed=True)
    with pytest.raises(ValueError):
        verdict_to_otlp_json(verdict, trace_id="not-hex")
    with pytest.raises(ValueError):
        verdict_to_otlp_json(verdict, trace_id="0" * 32)
    random_trace = verdict_to_otlp_json(verdict)["resourceSpans"][0]["scopeSpans"][0]
    assert len(random_trace["spans"][0]["traceId"]) == 32
    assert random_trace["spans"][0]["status"] == {"code": 0}


def test_cli_gate_json_carries_run_ids_and_writes_otlp(tmp_path, capsys):
    golden_file = tmp_path / "golden.json"
    candidate_file = tmp_path / "candidate.json"
    golden_file.write_text(json.dumps(_otlp(TRACE_GOLDEN, ("search", "verify"))))
    candidate_file.write_text(json.dumps(_otlp(TRACE_CANDIDATE, ("search",))))
    db = str(tmp_path / "traces.sqlite")
    assert main(["ingest", str(golden_file), str(candidate_file), "--db", db]) == 0
    capsys.readouterr()

    golden_id = str(run_id_for_trace(TRACE_GOLDEN))
    candidate_id = str(run_id_for_trace(TRACE_CANDIDATE))
    otlp_out = tmp_path / "verdict.otlp.json"
    code = main(
        ["gate", "--db", db, "--golden", golden_id, "--candidate", candidate_id,
         "--json", "--otlp-out", str(otlp_out)]
    )
    assert code == 1
    report = json.loads(capsys.readouterr().out)
    assert report["golden_run_id"] == golden_id
    assert report["candidate_run_id"] == candidate_id
    assert report["profile"] == "strict_audit_v1"

    verdict = GateVerdict.from_gate_json(report)
    assert verdict.candidate_run_id == candidate_id and verdict.passed is False

    request = json.loads(otlp_out.read_text())
    span = request["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    assert span["traceId"] == TRACE_CANDIDATE  # lands in the candidate's own trace
    attrs = _decode_attributes(span["attributes"])
    assert attrs["dpk.gate.verdict"] == "fail"
    assert attrs["dpk.candidate.run_id"] == candidate_id


# ── vendor adapters (recording fakes) ───────────────────────────────────────────


class Recorder:
    def __init__(self):
        self.calls = []

    def record(self, name, args, kwargs):
        self.calls.append((name, args, kwargs))


def _verdict():
    return GateVerdict(
        passed=False,
        regression_level="high",
        candidate_run_id=str(run_id_for_trace(TRACE_CANDIDATE)),
        removed_steps=("verify",),
        reasoning="Critical reasoning steps removed: verify",
    )


def test_langsmith_feedback_defaults_run_id_to_candidate():
    rec = Recorder()

    class Client:
        def create_feedback(self, run_id=None, key="unnamed", **kwargs):
            rec.record("create_feedback", (run_id, key), kwargs)
            return "ok"

    assert send_verdict_to_langsmith(_verdict(), client=Client()) == "ok"
    (_, (run_id, key), kwargs), = rec.calls
    assert run_id == uuid.UUID(TRACE_CANDIDATE) and key == "dpk_gate"
    assert kwargs["score"] == 0.0 and kwargs["value"] == "fail"
    assert "verify" in kwargs["comment"]
    assert kwargs["source_info"]["dpk.gate.steps.removed"] == ["verify"]
    assert "trace_id" not in kwargs  # None-valued optionals are never sent

    with pytest.raises(ValueError):
        send_verdict_to_langsmith(GateVerdict(passed=True), client=Client())


def test_langfuse_v3_create_score_and_v2_fallback():
    rec = Recorder()

    class V3:
        def create_score(self, *, name, value, trace_id=None, observation_id=None,
                         data_type=None, comment=None, metadata=None):
            rec.record("create_score", (), dict(name=name, value=value, trace_id=trace_id,
                       observation_id=observation_id, data_type=data_type,
                       comment=comment, metadata=metadata))

    class V2:
        def score(self, *, name, value, trace_id=None, observation_id=None,
                  data_type=None, comment=None):
            rec.record("score", (), dict(name=name, value=value, trace_id=trace_id,
                       data_type=data_type, comment=comment))

    send_verdict_to_langfuse(_verdict(), client=V3(), observation_id="c" * 16)
    send_verdict_to_langfuse(_verdict(), "explicit-trace", client=V2())
    (n1, _, k1), (n2, _, k2) = rec.calls
    assert n1 == "create_score" and k1["trace_id"] == TRACE_CANDIDATE
    assert k1["observation_id"] == "c" * 16
    assert k1["data_type"] == "BOOLEAN" and k1["value"] == 0.0
    assert k1["metadata"]["dpk.gate.verdict"] == "fail"
    assert n2 == "score" and k2["trace_id"] == "explicit-trace"  # metadata dropped on v2

    with pytest.raises(TypeError):
        send_verdict_to_langfuse(_verdict(), client=object())


def test_phoenix_span_annotation():
    rec = Recorder()

    class Spans:
        def add_span_annotation(self, *, span_id, annotation_name, annotator_kind="HUMAN",
                                label=None, score=None, explanation=None, metadata=None,
                                identifier=None, sync=False):
            rec.record("add_span_annotation", (), dict(
                span_id=span_id, annotation_name=annotation_name,
                annotator_kind=annotator_kind, label=label, score=score,
                explanation=explanation, metadata=metadata, sync=sync))

    class Client:
        spans = Spans()

    send_verdict_to_phoenix(_verdict(), "d" * 16, client=Client())
    (_, _, kwargs), = rec.calls
    assert kwargs["annotator_kind"] == "CODE" and kwargs["label"] == "fail"
    assert kwargs["score"] == 0.0 and kwargs["annotation_name"] == "dpk_gate"
    with pytest.raises(ValueError):
        send_verdict_to_phoenix(_verdict(), "", client=Client())


def test_datadog_evaluation_drops_kwargs_old_ddtrace_does_not_declare():
    rec = Recorder()

    class OldLLMObs:  # ddtrace before assessment/reasoning/metadata existed
        @staticmethod
        def submit_evaluation(label, metric_type, value, span=None,
                              span_with_tag_value=None, tags=None, ml_app=None,
                              timestamp_ms=None):
            rec.record("submit_evaluation", (), dict(label=label, metric_type=metric_type,
                       value=value, span=span, tags=tags))

    span_ref = {"span_id": "123", "trace_id": "456"}
    send_verdict_to_datadog(_verdict(), span_ref, llmobs=OldLLMObs, tags={"team": "ai"})
    (_, _, kwargs), = rec.calls
    assert kwargs["metric_type"] == "categorical" and kwargs["value"] == "fail"
    assert kwargs["span"] == span_ref
    assert kwargs["tags"]["dpk.gate.verdict"] == "fail" and kwargs["tags"]["team"] == "ai"

    with pytest.raises(ValueError):
        send_verdict_to_datadog(_verdict(), llmobs=OldLLMObs)
    with pytest.raises(ValueError):
        send_verdict_to_datadog(
            _verdict(), span_ref, span_with_tag_value={"tag_key": "k", "tag_value": "v"},
            llmobs=OldLLMObs,
        )


# ── real vendor signatures (only where the SDK is installed) ────────────────────


def _bound_against(real_fn, drop_self=True):
    """A fake whose signature is the real API's; records and validates every call."""
    sig = inspect.signature(real_fn)
    if drop_self:
        params = list(sig.parameters.values())[1:]
        sig = sig.replace(parameters=params)
    calls = []

    def fake(*args, **kwargs):
        sig.bind(*args, **kwargs)  # raises TypeError on any undeclared argument
        calls.append(kwargs)

    fake.__signature__ = sig
    return fake, calls


def test_langsmith_matches_real_create_feedback_signature():
    langsmith = pytest.importorskip("langsmith")
    fake, calls = _bound_against(langsmith.Client.create_feedback)

    class Client:
        create_feedback = staticmethod(fake)

    send_verdict_to_langsmith(_verdict(), client=Client())
    assert calls and calls[0]["value"] == "fail"


def test_langfuse_matches_real_create_score_signature():
    langfuse = pytest.importorskip("langfuse")
    if not hasattr(langfuse.Langfuse, "create_score"):
        pytest.skip("legacy Langfuse SDK")
    fake, calls = _bound_against(langfuse.Langfuse.create_score)

    class Client:
        create_score = staticmethod(fake)

    send_verdict_to_langfuse(_verdict(), client=Client())
    assert calls[0]["data_type"] == "BOOLEAN" and "metadata" in calls[0]


def test_phoenix_matches_real_add_span_annotation_signature():
    pytest.importorskip("phoenix.client")
    from phoenix.client.resources.spans import Spans

    fake, calls = _bound_against(Spans.add_span_annotation)

    class Client:
        class spans:  # noqa: N801 - mirrors the client attribute
            add_span_annotation = staticmethod(fake)

    send_verdict_to_phoenix(_verdict(), "d" * 16, client=Client())
    assert calls[0]["annotator_kind"] == "CODE"


def test_datadog_matches_real_submit_evaluation_signature():
    pytest.importorskip("ddtrace.llmobs")
    from ddtrace.llmobs import LLMObs

    fake, calls = _bound_against(LLMObs.submit_evaluation, drop_self=False)

    class FakeLLMObs:
        submit_evaluation = staticmethod(fake)

    send_verdict_to_datadog(_verdict(), {"span_id": "1", "trace_id": "2"}, llmobs=FakeLLMObs)
    assert calls[0]["metric_type"] == "categorical"


def test_otlp_json_parses_as_a_real_export_trace_service_request():
    pytest.importorskip("opentelemetry.proto")
    from google.protobuf import json_format
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
        ExportTraceServiceRequest,
    )

    verdict = GateVerdict(
        passed=False, strength=1.0, removed_steps=("verify",),
        candidate_run_id=str(run_id_for_trace(TRACE_CANDIDATE)),
    )
    request = verdict_to_otlp_json(verdict, parent_span_id="e" * 16)
    # OTLP/JSON mandates hex ids while protobuf's generic JSON mapping expects base64
    # for bytes fields (the known OTLP/JSON deviation), so swap the ids out before
    # validating every other field against the real protobuf schema.
    span_json = request["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    assert span_json["traceId"] == TRACE_CANDIDATE
    assert span_json["parentSpanId"] == "e" * 16
    for key in ("traceId", "spanId", "parentSpanId"):
        span_json.pop(key)
    message = json_format.ParseDict(request, ExportTraceServiceRequest())
    span = message.resource_spans[0].scope_spans[0].spans[0]
    assert span.name == "dpk.gate" and span.status.code == 2
    removed = {kv.key: kv.value for kv in span.attributes}["dpk.gate.steps.removed"]
    assert [v.string_value for v in removed.array_value.values] == ["verify"]

"""Attach DProvenanceKit gate verdicts to OpenTelemetry traces.

Three entry points, from most to least integrated:

* :func:`attach_verdict` puts the ``dpk.*`` attributes, a ``dpk.gate.verdict`` span
  event and the standard GenAI ``gen_ai.evaluation.result`` event on a span you already
  have (``opentelemetry.trace.get_current_span()``, a
  Langfuse/Logfire/Phoenix-instrumented span, or anything with the OTel ``Span`` API).
* :func:`record_verdict_span` opens a dedicated ``dpk.gate`` span on a tracer you pass in,
  so the verdict flows through whatever exporter that tracer provider already uses
  (OTLP to Phoenix, Langfuse's OTel endpoint, the Datadog Agent, a Collector, …).
* :func:`verdict_to_otlp_json` builds an OTLP/HTTP JSON ``ExportTraceServiceRequest``
  with the standard library only, so a CI job with no OpenTelemetry SDK installed can
  ``curl`` a verdict to any OTLP endpoint. When the candidate run was imported from an
  OTel trace (``dpk ingest`` / ``dpk init --from-otel``) its run id *is* that trace id,
  so the verdict span lands inside the original trace by default.

None of these import ``opentelemetry`` at module import time; only
:func:`record_verdict_span` touches it, and only to set an error status when available.
Install the optional extra with ``pip install "dprovenancekit[otel]"``.
"""

from __future__ import annotations

import secrets
import time
import uuid
from typing import Any, Dict, List, Mapping, Optional

from .verdict import AttributeValue, GateVerdict

#: Name of the span event carrying the verdict on an existing span.
VERDICT_EVENT_NAME = "dpk.gate.verdict"
#: The standard OTel GenAI evaluation event (semantic-conventions-genai, Development).
EVALUATION_EVENT_NAME = "gen_ai.evaluation.result"
#: Default name of the dedicated span created by :func:`record_verdict_span`.
VERDICT_SPAN_NAME = "dpk.gate"
#: Instrumentation scope name used for OTLP output.
INSTRUMENTATION_SCOPE = "dprovenancekit"


def attach_verdict(
    span: Any,
    verdict: GateVerdict,
    *,
    add_event: bool = True,
    set_attributes: bool = True,
    evaluation_event: bool = True,
) -> None:
    """Attach ``verdict`` to an existing OpenTelemetry span.

    Args:
        span: any object implementing the OTel ``Span`` API subset ``set_attribute`` /
            ``add_event`` (``opentelemetry.trace.Span`` and SDK spans do).
        verdict: the verdict to attach.
        add_event: record a ``dpk.gate.verdict`` span event carrying the attributes.
        set_attributes: also set the attributes directly on the span, so backends that
            index span attributes (but not event attributes) can filter on them.
        evaluation_event: also record the standard GenAI ``gen_ai.evaluation.result``
            event (binary pass/fail evaluation), so convention-aware backends that know
            nothing about ``dpk.*`` still show the verdict.

    The span's status is never modified: whether a regression should mark the
    surrounding operation as failed is the caller's decision.
    """
    attrs = verdict.to_attributes()
    if set_attributes:
        for key, value in attrs.items():
            span.set_attribute(key, value)
    if add_event:
        span.add_event(VERDICT_EVENT_NAME, attributes=dict(attrs))
    if evaluation_event:
        span.add_event(EVALUATION_EVENT_NAME, attributes=verdict.evaluation_result_attributes())


def record_verdict_span(
    tracer: Any,
    verdict: GateVerdict,
    *,
    name: str = VERDICT_SPAN_NAME,
    error_on_fail: bool = True,
    evaluation_event: bool = True,
) -> Any:
    """Emit ``verdict`` as a dedicated span on ``tracer`` and return the ended span.

    The span is started with ``tracer.start_as_current_span`` (so it nests under the
    caller's current span when there is one), carries the ``dpk.*`` attributes plus a
    ``dpk.gate.verdict`` event and (unless ``evaluation_event`` is off) the standard
    ``gen_ai.evaluation.result`` event, and, when ``error_on_fail`` is set and the gate
    failed, gets ``StatusCode.ERROR`` with the verdict summary as the description.
    """
    attrs = verdict.to_attributes()
    with tracer.start_as_current_span(name, attributes=dict(attrs)) as span:
        span.add_event(VERDICT_EVENT_NAME, attributes=dict(attrs))
        if evaluation_event:
            span.add_event(
                EVALUATION_EVENT_NAME, attributes=verdict.evaluation_result_attributes()
            )
        if error_on_fail and not verdict.passed:
            _set_error_status(span, verdict.summary())
    return span


def _set_error_status(span: Any, description: str) -> None:
    try:
        from opentelemetry.trace import Status, StatusCode
    except ImportError:  # pragma: no cover - only without opentelemetry-api installed
        return
    span.set_status(Status(StatusCode.ERROR, description))


# ── OTLP/HTTP JSON (stdlib only) ────────────────────────────────────────────────


def _any_value(value: AttributeValue) -> Dict[str, Any]:
    # bool before int: bool is an int subclass in Python.
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}  # proto3 JSON encodes int64 as a string
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, (list, tuple)):
        return {"arrayValue": {"values": [_any_value(v) for v in value]}}
    return {"stringValue": str(value)}


def _key_values(attrs: Mapping[str, AttributeValue]) -> List[Dict[str, Any]]:
    return [{"key": key, "value": _any_value(value)} for key, value in attrs.items()]


def _hex_id(value: Optional[str], length: int) -> Optional[str]:
    """Normalize a hex id (or a UUID string) to ``length`` lowercase hex characters."""
    if not value:
        return None
    text = str(value).strip().lower()
    try:
        text = uuid.UUID(text).hex
    except ValueError:
        pass
    if len(text) != length or text.strip("0123456789abcdef"):
        raise ValueError(f"expected a {length}-character hex id, got {value!r}")
    if text == "0" * length:
        raise ValueError("an all-zero id is invalid in OpenTelemetry")
    return text


def verdict_to_otlp_json(
    verdict: GateVerdict,
    *,
    trace_id: Optional[str] = None,
    parent_span_id: Optional[str] = None,
    span_id: Optional[str] = None,
    service_name: str = "dprovenancekit-gate",
    name: str = VERDICT_SPAN_NAME,
    start_time_ns: Optional[int] = None,
    end_time_ns: Optional[int] = None,
    error_on_fail: bool = True,
    evaluation_event: bool = True,
    resource_attributes: Optional[Mapping[str, AttributeValue]] = None,
) -> Dict[str, Any]:
    """Build an OTLP/HTTP JSON ``ExportTraceServiceRequest`` holding one verdict span.

    Args:
        trace_id: 32-hex OTel trace id (a UUID string is accepted). Defaults to the
            verdict's ``candidate_run_id`` when that is a UUID, which re-joins the trace
            the candidate run was imported from, else a fresh random id.
        parent_span_id: optional 16-hex parent span id within that trace.
        span_id: optional 16-hex span id (random by default).
        service_name: ``service.name`` resource attribute.
        error_on_fail: set span status ``ERROR`` (code 2) when the gate failed.
        evaluation_event: also include the standard ``gen_ai.evaluation.result`` event.
        resource_attributes: extra resource attributes (for example
            ``deployment.environment.name``).

    The result is ready for ``json.dumps`` and a ``POST`` to ``<otlp-endpoint>/v1/traces``
    with ``Content-Type: application/json``.
    """
    resolved_trace = _hex_id(trace_id, 32)
    if resolved_trace is None and verdict.candidate_run_id:
        try:
            resolved_trace = _hex_id(verdict.candidate_run_id, 32)
        except ValueError:
            resolved_trace = None
    if resolved_trace is None:
        resolved_trace = secrets.token_hex(16)
    resolved_span = _hex_id(span_id, 16) or secrets.token_hex(8)
    parent = _hex_id(parent_span_id, 16)

    end_ns = end_time_ns if end_time_ns is not None else time.time_ns()
    start_ns = start_time_ns if start_time_ns is not None else end_ns
    attrs = verdict.to_attributes()

    span: Dict[str, Any] = {
        "traceId": resolved_trace,
        "spanId": resolved_span,
        "name": name,
        "kind": 1,  # SPAN_KIND_INTERNAL
        "startTimeUnixNano": str(start_ns),
        "endTimeUnixNano": str(end_ns),
        "attributes": _key_values(attrs),
        "events": [
            {
                "timeUnixNano": str(end_ns),
                "name": VERDICT_EVENT_NAME,
                "attributes": _key_values(attrs),
            }
        ],
        "status": (
            {"code": 2, "message": verdict.summary()}
            if error_on_fail and not verdict.passed
            else {"code": 0}
        ),
    }
    if evaluation_event:
        span["events"].append(
            {
                "timeUnixNano": str(end_ns),
                "name": EVALUATION_EVENT_NAME,
                "attributes": _key_values(verdict.evaluation_result_attributes()),
            }
        )
    if parent is not None:
        span["parentSpanId"] = parent

    resource: Dict[str, AttributeValue] = {"service.name": service_name}
    if resource_attributes:
        resource.update(resource_attributes)
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": _key_values(resource)},
                "scopeSpans": [
                    {
                        "scope": {
                            "name": INSTRUMENTATION_SCOPE,
                            "version": str(attrs.get("dpk.sdk.version", "")),
                        },
                        "spans": [span],
                    }
                ],
            }
        ]
    }


__all__ = [
    "EVALUATION_EVENT_NAME",
    "INSTRUMENTATION_SCOPE",
    "VERDICT_EVENT_NAME",
    "VERDICT_SPAN_NAME",
    "attach_verdict",
    "record_verdict_span",
    "verdict_to_otlp_json",
]

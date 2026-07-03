"""Ingest OpenTelemetry GenAI traces (OTLP/HTTP JSON) as DProvenanceKit runs.

Any agent stack instrumented for OpenTelemetry — via the official **GenAI semantic
conventions** (``gen_ai.*``), **OpenInference** (Arize Phoenix instrumentations), or
**OpenLLMetry/Traceloop** — can export its traces as OTLP JSON. :func:`ingest_otlp`
turns those exported traces into ordinary DProvenanceKit runs, so the whole toolkit
(query, diff, run fingerprints, anomaly rules, the ``gate`` CLI) applies to traces
DProvenanceKit did not record itself::

    from dprovenancekit import SQLiteTraceStore
    from dprovenancekit.otel_ingest import OTelSpanEvent, ingest_otlp

    store = SQLiteTraceStore(OTelSpanEvent, "traces.sqlite")
    runs = ingest_otlp("exported_traces.json", store)
    store.close()

or from the command line (then gate as usual)::

    dprovenancekit ingest --db traces.sqlite golden.json candidate.json
    dprovenancekit gate --db traces.sqlite --golden <run> --candidate <run>

**Input.** The OTLP/HTTP JSON encoding of ``ExportTraceServiceRequest`` — what the OTel
Collector's file exporter writes (one JSON object per line), what ``curl``'ing an OTLP
endpoint sends, and what most SDK file/console exporters produce. A file may hold a
single request object, a JSON array of them, or JSON-lines. Parsed with the stdlib only.

**Mapping.** Each OTel *trace* becomes one run; the run id is derived deterministically
from the 128-bit trace id, so re-ingesting the same trace is detectable (and skippable)
rather than duplicating. Each *span* is classified into a vendor-neutral step kind —
``llm_call``, ``tool_call``, ``agent_invocation``, ``chain``, ``retrieval``,
``embedding``, … — and becomes a ``<kind>.start`` / ``<kind>.end`` event pair (or
``<kind>.error`` when the span status is error), mirroring the live integrations. The
active component (model, tool, or agent name) becomes the **engine**, so diff signatures
(``type::engine``) and fingerprints are stable across instrumentation dialects: a
LangChain agent traced via OpenInference and the same agent traced via the official
``gen_ai.*`` conventions normalize to comparable step streams.

**Ordering.** OTLP spans arrive complete, so causal order is *reconstructed*: spans are
arranged as a tree (``parentSpanId``), siblings sorted by start time, and events emitted
by depth-first walk — ``parent.start``, children, ``parent.end``. This canonical nesting
is deterministic even when wall-clock timestamps tie, which sequence-based diffing needs.

Spans that match no known GenAI dialect are dropped by default (they would pollute run
fingerprints); pass ``include_unclassified=True`` to keep them as ``span.*`` telemetry
events, which stay invisible to structural diffs but visible to queries.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import (
    Any,
    Dict,
    FrozenSet,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
)

from .event import TraceableEvent, TraceEvent
from .priority import TracePriority

# ── Event type ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class OTelSpanEvent(TraceableEvent):
    """A span lifecycle event ingested from an OpenTelemetry trace.

    Parallel to the live integrations' event types: attributes are stored as a canonical
    (sorted-key) JSON string so the event is hashable and two events with the same
    logical attributes compare equal (which makes exact-equality alignment work).
    """

    type_name: str
    priority_value: int
    attributes_json: str = "{}"

    @classmethod
    def make(
        cls,
        type_name: str,
        priority: TracePriority,
        attributes: Optional[Mapping[str, Any]] = None,
    ) -> "OTelSpanEvent":
        clean = {k: v for k, v in (attributes or {}).items() if v is not None}
        return cls(
            type_name=type_name,
            priority_value=int(priority),
            attributes_json=json.dumps(clean, sort_keys=True, default=str),
        )

    @property
    def type_identifier(self) -> str:
        return self.type_name

    @property
    def priority(self) -> TracePriority:
        try:
            return TracePriority(self.priority_value)
        except ValueError:
            return TracePriority.TELEMETRY

    @property
    def attributes(self) -> Dict[str, Any]:
        return json.loads(self.attributes_json)

    def to_dict(self) -> dict:
        out: Dict[str, Any] = {"type": self.type_name, "priority": self.priority_value}
        out.update(json.loads(self.attributes_json))
        return out

    @classmethod
    def from_dict(cls, data: dict) -> "OTelSpanEvent":
        attrs = {k: v for k, v in data.items() if k not in ("type", "priority")}
        return cls.make(
            type_name=data["type"],
            priority=TracePriority(
                int(data.get("priority", int(TracePriority.STRUCTURAL)))
            ),
            attributes=attrs,
        )


# ── OTLP JSON decoding ───────────────────────────────────────────────────────────
#
# The OTLP/HTTP JSON encoding (opentelemetry-proto) nests resourceSpans → scopeSpans →
# spans, camelCases field names, renders trace/span ids as hex strings, encodes 64-bit
# integers (including *nano timestamps and intValue attributes*) as JSON strings, and
# tags attribute values: {"stringValue": "x"} / {"intValue": "3"} / {"doubleValue": 0.5}
# / {"boolValue": true} / {"arrayValue": {"values": […]}} / {"kvlistValue": {…}}.


def _decode_any_value(value: Any) -> Any:
    """Decode one OTLP ``AnyValue`` into a plain Python value."""
    if not isinstance(value, Mapping):
        return value
    if "stringValue" in value:
        return value["stringValue"]
    if "intValue" in value:
        try:
            return int(value["intValue"])  # JSON encodes int64 as a string
        except (TypeError, ValueError):
            return value["intValue"]
    if "doubleValue" in value:
        return value["doubleValue"]
    if "boolValue" in value:
        return value["boolValue"]
    if "arrayValue" in value:
        inner = value["arrayValue"]
        values = inner.get("values", []) if isinstance(inner, Mapping) else []
        return [_decode_any_value(v) for v in values]
    if "kvlistValue" in value:
        inner = value["kvlistValue"]
        values = inner.get("values", []) if isinstance(inner, Mapping) else []
        return _decode_attributes(values)
    if "bytesValue" in value:
        return value["bytesValue"]  # base64 string; opaque is fine here
    return value


def _decode_attributes(attr_list: Any) -> Dict[str, Any]:
    """Decode an OTLP ``KeyValue`` list into a flat dict (last write wins)."""
    out: Dict[str, Any] = {}
    if not isinstance(attr_list, Sequence):
        return out
    for kv in attr_list:
        if isinstance(kv, Mapping) and "key" in kv:
            out[str(kv["key"])] = _decode_any_value(kv.get("value"))
    return out


def _decode_nanos(value: Any) -> int:
    """Decode a unixNano timestamp (JSON string or number) to an int, 0 if absent."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


_STATUS_ERROR_CODES = {2, "2", "STATUS_CODE_ERROR", "ERROR"}


@dataclass
class _OTelSpan:
    """One decoded OTLP span, flattened for classification."""

    trace_id: str
    span_id: str
    parent_span_id: Optional[str]
    name: str
    start_ns: int
    end_ns: int
    is_error: bool
    status_message: Optional[str]
    attributes: Dict[str, Any]
    resource: Dict[str, Any]
    children: List["_OTelSpan"] = field(default_factory=list)


def _field(obj: Mapping[str, Any], camel: str, snake: str, default: Any = None) -> Any:
    """Read an OTLP field leniently: camelCase per spec, snake_case from non-compliant
    exporters (protobuf json_format, older collector debug output)."""
    if camel in obj:
        return obj[camel]
    return obj.get(snake, default)


def _decode_spans(request: Mapping[str, Any]) -> List[_OTelSpan]:
    """Flatten one ``ExportTraceServiceRequest`` JSON object into spans."""
    spans: List[_OTelSpan] = []
    for resource_spans in _field(request, "resourceSpans", "resource_spans", []) or []:
        if not isinstance(resource_spans, Mapping):
            continue
        resource_obj = resource_spans.get("resource")
        resource = _decode_attributes(
            resource_obj.get("attributes") if isinstance(resource_obj, Mapping) else None
        )
        for scope_spans in _field(resource_spans, "scopeSpans", "scope_spans", []) or []:
            if not isinstance(scope_spans, Mapping):
                continue
            for span in scope_spans.get("spans", []) or []:
                if not isinstance(span, Mapping):
                    continue
                trace_id = str(_field(span, "traceId", "trace_id", "") or "")
                span_id = str(_field(span, "spanId", "span_id", "") or "")
                if not trace_id or not span_id:
                    continue  # ids are mandatory; a span without them is unusable
                parent = str(_field(span, "parentSpanId", "parent_span_id", "") or "")
                status = span.get("status") or {}
                status_code = (
                    status.get("code") if isinstance(status, Mapping) else None
                )
                # Keep ids verbatim: a hex id is case-normalized downstream by
                # run_id_for_trace, but a base64 id (protobuf json_format output) is
                # case-sensitive and must not be lowercased. Casing is consistent within
                # one exporter's file, so span/parent matching is unaffected.
                spans.append(
                    _OTelSpan(
                        trace_id=trace_id,
                        span_id=span_id,
                        parent_span_id=parent or None,
                        name=str(span.get("name", "") or ""),
                        start_ns=_decode_nanos(
                            _field(span, "startTimeUnixNano", "start_time_unix_nano")
                        ),
                        end_ns=_decode_nanos(
                            _field(span, "endTimeUnixNano", "end_time_unix_nano")
                        ),
                        is_error=status_code in _STATUS_ERROR_CODES,
                        status_message=(
                            str(status["message"])
                            if isinstance(status, Mapping) and status.get("message")
                            else None
                        ),
                        attributes=_decode_attributes(span.get("attributes")),
                        resource=resource,
                    )
                )
    return spans


def _parse_otlp_text(text: str) -> List[Mapping[str, Any]]:
    """Parse OTLP JSON text: one request object, an array of them, or JSON-lines."""
    stripped = text.strip()
    if not stripped:
        return []
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        requests: List[Mapping[str, Any]] = []
        for lineno, line in enumerate(stripped.splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"line {lineno} is not valid JSON: {exc}"
                ) from exc
            if isinstance(obj, Mapping):
                requests.append(obj)
        return requests
    if isinstance(parsed, Mapping):
        return [parsed]
    if isinstance(parsed, list):
        return [obj for obj in parsed if isinstance(obj, Mapping)]
    return []


# ── Classification ─────────────────────────────────────────────────────────────
#
# Dialect detection is ordered by specificity (see _classify for the exact sequence): an
# explicit OpenInference span kind wins, then Traceloop's workflow markers, then the
# official ``gen_ai.operation.name``, then legacy ``llm.request.type``, then the Vercel AI
# SDK operation id, then a best-effort sniff of well-known ``gen_ai.*`` attributes, and
# finally the SHOULD-level span-name prefix. Each classifier returns a vendor-neutral step
# kind plus the "engine" — the stable component identity (model, tool, or agent name) that
# diff signatures and fingerprints key on.

LLM_CALL = "llm_call"
TOOL_CALL = "tool_call"
AGENT_INVOCATION = "agent_invocation"
AGENT_CREATION = "agent_creation"
CHAIN = "chain"
RETRIEVAL = "retrieval"
EMBEDDING = "embedding"
RERANK = "rerank"
GUARDRAIL = "guardrail"
UNCLASSIFIED = "span"

# openinference.span.kind → step kind (OpenInference semantic conventions, Arize).
# The spec is living and has added kinds over time (PROMPT, EVALUATOR, …); any
# unrecognized kind maps to a generic CHAIN step rather than being dropped.
_OPENINFERENCE_KINDS = {
    "LLM": LLM_CALL,
    "TOOL": TOOL_CALL,
    "AGENT": AGENT_INVOCATION,
    "CHAIN": CHAIN,
    "RETRIEVER": RETRIEVAL,
    "EMBEDDING": EMBEDDING,
    "RERANKER": RERANK,
    "GUARDRAIL": GUARDRAIL,
    "EVALUATOR": CHAIN,
    "PROMPT": CHAIN,
}

# gen_ai.operation.name → step kind (official OTel GenAI semantic conventions,
# semconv v1.41.1 / semantic-conventions-genai as of mid-2026).
_GENAI_OPERATIONS = {
    "chat": LLM_CALL,
    "text_completion": LLM_CALL,
    "generate_content": LLM_CALL,
    "embeddings": EMBEDDING,
    "retrieval": RETRIEVAL,
    "execute_tool": TOOL_CALL,
    "invoke_agent": AGENT_INVOCATION,
    "invoke_workflow": CHAIN,
    "create_agent": AGENT_CREATION,
}

# traceloop.span.kind → step kind (OpenLLMetry workflow layer, all versions). LLM calls
# under OpenLLMetry carry ``gen_ai.*`` / ``llm.request.type`` instead of a traceloop
# kind, so they fall through to the later checks.
_TRACELOOP_KINDS = {
    "tool": TOOL_CALL,
    "agent": AGENT_INVOCATION,
    "workflow": CHAIN,
    "task": CHAIN,
}

# llm.request.type → step kind (legacy OpenLLMetry LLM spans, still the most common
# GenAI dialect in exported files as of mid-2026).
_LLM_REQUEST_TYPES = {
    "chat": LLM_CALL,
    "completion": LLM_CALL,
    "embedding": EMBEDDING,
    "rerank": RERANK,
}

# ai.operationId / operation.name prefixes → step kind (Vercel AI SDK telemetry).
_VERCEL_OPERATIONS = (
    ("ai.toolCall", TOOL_CALL),
    ("ai.embed", EMBEDDING),
    ("ai.generate", LLM_CALL),
    ("ai.stream", LLM_CALL),
)

# Attribute keys consulted for the engine, in precedence order, per step kind. Both
# current and deprecated/renamed spellings appear because real exporters emit several
# generations at once (gen_ai.system → gen_ai.provider.name; OpenLLMetry's legacy keys;
# OpenInference and Vercel dialect keys).
_ENGINE_KEYS: Dict[str, Tuple[str, ...]] = {
    LLM_CALL: (
        "gen_ai.request.model",
        "llm.model_name",
        "ai.model.id",
        "gen_ai.response.model",
        "gen_ai.provider.name",
        "gen_ai.system",
    ),
    EMBEDDING: (
        "gen_ai.request.model",
        "embedding.model_name",
        "llm.model_name",
        "ai.model.id",
        "gen_ai.provider.name",
        "gen_ai.system",
    ),
    TOOL_CALL: (
        "gen_ai.tool.name",
        "tool.name",
        "traceloop.entity.name",
        "ai.toolCall.name",
    ),
    AGENT_INVOCATION: ("gen_ai.agent.name", "agent.name", "traceloop.entity.name"),
    AGENT_CREATION: ("gen_ai.agent.name", "agent.name"),
    CHAIN: ("gen_ai.workflow.name", "traceloop.entity.name", "graph.node.name"),
    RETRIEVAL: ("gen_ai.data_source.id", "embedding.model_name"),
    RERANK: ("reranker.model_name", "gen_ai.request.model", "llm.model_name"),
    GUARDRAIL: ("guardrail.name", "gen_ai.tool.name", "tool.name"),
}

# Operation-name prefixes the official conventions put in span names ("execute_tool
# search", "invoke_agent Planner", "chat gpt-5"). Used two ways: stripping the prefix
# off the span name recovers the component identity when the attribute is missing, and
# a matching name classifies attribute-less spans (name-based dispatch is SHOULD-level
# only, so it is the last resort, never the primary signal).
_NAME_PREFIX_KINDS = {
    "execute_tool": TOOL_CALL,
    "invoke_agent": AGENT_INVOCATION,
    "create_agent": AGENT_CREATION,
    "invoke_workflow": CHAIN,
    "chat": LLM_CALL,
    "embeddings": EMBEDDING,
    "text_completion": LLM_CALL,
    "generate_content": LLM_CALL,
    "retrieval": RETRIEVAL,
}
_NAME_PREFIXES = tuple(f"{op} " for op in _NAME_PREFIX_KINDS)


def _vercel_operation(attrs: Mapping[str, Any]) -> Optional[str]:
    op = attrs.get("ai.operationId") or attrs.get("operation.name")
    if not isinstance(op, str):
        return None
    # Plain prefix match: operation ids compose ("ai.generateText.doGenerate",
    # "ai.embedMany.doEmbed") and the table is ordered most-specific first.
    for prefix, kind in _VERCEL_OPERATIONS:
        if op.startswith(prefix):
            return kind
    return None


def _classify(span: _OTelSpan) -> Optional[str]:
    """The vendor-neutral step kind for a span, or ``None`` if it isn't GenAI.

    Detection precedence (most reliable discriminator first): OpenInference's span kind,
    Traceloop's workflow-layer kind, the official ``gen_ai.operation.name``, legacy
    OpenLLMetry's ``llm.request.type``, the Vercel AI SDK operation id, then best-effort
    sniffing of well-known ``gen_ai.*`` keys, and finally span-name prefixes.
    """
    attrs = span.attributes

    oi_kind = attrs.get("openinference.span.kind")
    if isinstance(oi_kind, str) and oi_kind:
        # Unknown kinds (the spec keeps growing) map to a generic step, not a drop.
        return _OPENINFERENCE_KINDS.get(oi_kind.upper(), CHAIN)

    tl_kind = attrs.get("traceloop.span.kind")
    if isinstance(tl_kind, str) and tl_kind.lower() in _TRACELOOP_KINDS:
        return _TRACELOOP_KINDS[tl_kind.lower()]

    operation = attrs.get("gen_ai.operation.name")
    if isinstance(operation, str) and operation:
        if operation in _GENAI_OPERATIONS:
            return _GENAI_OPERATIONS[operation]
        return LLM_CALL if "gen_ai.request.model" in attrs else CHAIN

    request_type = attrs.get("llm.request.type")
    if isinstance(request_type, str) and request_type.lower() in _LLM_REQUEST_TYPES:
        return _LLM_REQUEST_TYPES[request_type.lower()]

    vercel = _vercel_operation(attrs)
    if vercel is not None:
        return vercel

    # Best-effort sniff: instrumentations predating gen_ai.operation.name.
    if "gen_ai.tool.name" in attrs:
        return TOOL_CALL
    if "gen_ai.agent.name" in attrs:
        return AGENT_INVOCATION
    if any(
        key in attrs
        for key in (
            "gen_ai.request.model",
            "gen_ai.response.model",
            "gen_ai.usage.input_tokens",
            "gen_ai.usage.prompt_tokens",
            "gen_ai.system",
        )
    ) or any(key.startswith("gen_ai.prompt.") for key in attrs):
        return LLM_CALL

    # Last resort: the SHOULD-level span-name pattern "{operation} {target}".
    for prefix, kind in _NAME_PREFIX_KINDS.items():
        if span.name.startswith(prefix + " ") and len(span.name) > len(prefix) + 1:
            return kind
    return None


def _engine_for(span: _OTelSpan, kind: str) -> str:
    """The component identity (model / tool / agent name) diff signatures key on."""
    for key in _ENGINE_KEYS.get(kind, ()):
        value = span.attributes.get(key)
        if value is not None and str(value):
            return str(value)
    name = span.name
    for prefix in _NAME_PREFIXES:
        if name.startswith(prefix) and len(name) > len(prefix):
            return name[len(prefix) :]
    return name or kind


def _truncate(text: str, limit: int = 2000) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


# Curated identity attributes carried onto both the .start and .end events.
_IDENTITY_KEYS = (
    ("model", ("gen_ai.request.model", "llm.model_name")),
    ("provider", ("gen_ai.provider.name", "gen_ai.system", "llm.provider")),
    ("tool", ("gen_ai.tool.name", "tool.name")),
    ("tool_call_id", ("gen_ai.tool.call.id",)),
    ("agent", ("gen_ai.agent.name",)),
)

# Result attributes carried only onto the .end event.
_RESULT_KEYS = (
    ("response_model", ("gen_ai.response.model",)),
    (
        "input_tokens",
        ("gen_ai.usage.input_tokens", "gen_ai.usage.prompt_tokens", "llm.token_count.prompt"),
    ),
    (
        "output_tokens",
        (
            "gen_ai.usage.output_tokens",
            "gen_ai.usage.completion_tokens",
            "llm.token_count.completion",
        ),
    ),
    ("finish_reasons", ("gen_ai.response.finish_reasons",)),
)

# Payload previews (opt-out via capture_payloads=False), by convention generation:
# current gen_ai.* message attributes, OpenInference input/output values, legacy JSON
# strings, and — reassembled from flattened per-index keys — legacy OpenLLMetry
# (gen_ai.prompt.{i}.content) and OpenInference (llm.input_messages.{i}.message.content)
# chat shapes. Previews are display-only: they never contribute to structural identity
# (instrumentors redact/truncate them at will).
_INPUT_PREVIEW_KEYS = ("gen_ai.input.messages", "input.value", "gen_ai.prompt")
_OUTPUT_PREVIEW_KEYS = ("gen_ai.output.messages", "output.value", "gen_ai.completion")
_INPUT_INDEXED = (
    ("gen_ai.prompt", ".content"),
    ("llm.input_messages", ".message.content"),
)
_OUTPUT_INDEXED = (
    ("gen_ai.completion", ".content"),
    ("llm.output_messages", ".message.content"),
)


def _pick(attrs: Mapping[str, Any], keys: Sequence[str]) -> Optional[Any]:
    for key in keys:
        if key in attrs and attrs[key] is not None:
            return attrs[key]
    return None


def _preview(
    attrs: Mapping[str, Any],
    direct_keys: Sequence[str],
    indexed: Sequence[Tuple[str, str]],
) -> Optional[str]:
    value = _pick(attrs, direct_keys)
    if value is not None:
        return value if isinstance(value, str) else json.dumps(value, default=str)
    for prefix, suffix in indexed:
        parts: List[str] = []
        for i in range(64):  # flattened indices are dense; stop at the first gap
            item = attrs.get(f"{prefix}.{i}{suffix}")
            if item is None:
                break
            parts.append(str(item))
        if parts:
            return "\n".join(parts)
    return None


def _identity_attributes(span: _OTelSpan) -> Dict[str, Any]:
    out: Dict[str, Any] = {"name": span.name} if span.name else {}
    for attr_name, keys in _IDENTITY_KEYS:
        value = _pick(span.attributes, keys)
        if value is not None:
            out[attr_name] = value
    return out


def _result_attributes(span: _OTelSpan, capture_payloads: bool) -> Dict[str, Any]:
    out = _identity_attributes(span)
    for attr_name, keys in _RESULT_KEYS:
        value = _pick(span.attributes, keys)
        if value is not None:
            out[attr_name] = value
    if capture_payloads:
        output = _preview(span.attributes, _OUTPUT_PREVIEW_KEYS, _OUTPUT_INDEXED)
        if output is not None:
            out["output"] = _truncate(output)
    return out


# ── Tree reconstruction and run assembly ─────────────────────────────────────────


def _dedupe_spans(spans: List[_OTelSpan]) -> List[_OTelSpan]:
    """Collapse spans re-delivered under the same span id (OTLP allows at-least-once
    delivery; deduplication is the receiver's job). The instance with the greatest
    end time wins — the most-complete copy — with input order breaking ties."""
    best: Dict[str, Tuple[int, int, _OTelSpan]] = {}
    for index, span in enumerate(spans):
        key = span.span_id
        rank = (span.end_ns, index)
        existing = best.get(key)
        if existing is None or rank > existing[:2]:
            best[key] = (rank[0], rank[1], span)
    if len(best) == len(spans):
        return spans  # no duplicates — preserve identity/order exactly
    return [entry[2] for entry in best.values()]


def _build_forest(spans: List[_OTelSpan]) -> List[_OTelSpan]:
    """Arrange one trace's spans as a forest; orphans (missing parents) become roots."""
    spans = _dedupe_spans(spans)
    by_id = {span.span_id: span for span in spans}
    roots: List[_OTelSpan] = []
    for span in spans:
        parent = by_id.get(span.parent_span_id) if span.parent_span_id else None
        if parent is None or parent is span:
            roots.append(span)
        else:
            parent.children.append(span)
    order = lambda s: (s.start_ns, s.span_id)  # noqa: E731 — one sort key, used twice
    roots.sort(key=order)
    for span in spans:
        span.children.sort(key=order)
    return roots


def run_id_for_trace(trace_id: str) -> uuid.UUID:
    """The deterministic DProvenanceKit run id for an OTel trace id.

    Total by construction — never raises, so one malformed id cannot abort a whole
    file. A spec-compliant 32-hex id maps to the matching UUID. A base64-encoded id
    (what ``protobuf`` ``json_format`` emits for the bytes-typed ``traceId``, rather
    than the hex the OTLP/JSON spec mandates) is decoded to its 16 bytes so it yields
    the *same* run id as the hex form of those bytes. Anything else is hashed with a
    fixed namespace, keeping ingestion deterministic per exporter.
    """
    raw = (trace_id or "").strip()
    lowered = raw.lower()
    hex_only = lowered.lstrip("0123456789abcdef")
    if raw and not hex_only and len(lowered) <= 32:
        return uuid.UUID(hex=lowered.zfill(32))
    for decoder in (_b64_std, _b64_url):
        decoded = decoder(raw)
        if decoded is not None and len(decoded) == 16:
            return uuid.UUID(bytes=decoded)
    return uuid.uuid5(_TRACE_ID_NAMESPACE, raw)


# Fixed namespace so non-hex, non-16-byte trace ids still map deterministically.
_TRACE_ID_NAMESPACE = uuid.UUID("6f1a0f2e-2b8c-5e3a-9d47-0e10a1b2c3d4")


def _b64_std(text: str) -> Optional[bytes]:
    import base64
    import binascii

    try:
        return base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        return None


def _b64_url(text: str) -> Optional[bytes]:
    import base64
    import binascii

    try:
        return base64.urlsafe_b64decode(text)
    except (binascii.Error, ValueError):
        return None


@dataclass(frozen=True)
class IngestedRun:
    """Summary of one OTel trace ingested as a run."""

    run_id: uuid.UUID
    trace_id: str
    context_id: str
    event_count: int


def _emit_forest_events(
    roots: List[_OTelSpan],
    *,
    run_id: uuid.UUID,
    context_id: str,
    include_unclassified: bool,
    capture_payloads: bool,
    schema_version: int,
    sink: List[TraceEvent],
) -> None:
    """Depth-first walk of a whole forest: for each span emit ``<kind>.start``, then
    its children (already sorted by start time), then ``<kind>.end`` / ``.error``.

    Iterative (explicit stack) rather than recursive so that deep span trees — a long
    chain of nested tool/agent calls — cannot exhaust Python's recursion limit. A
    ``visited`` set makes a malformed parent cycle terminate instead of looping.
    """

    def _append(span, type_name, prio, attrs, engine, t_ns):
        sink.append(
            TraceEvent(
                run_id=run_id,
                context_id=context_id,
                engine_name=engine,
                schema_version=schema_version,
                sequence=len(sink),
                span_id=span.span_id,
                parent_span_id=span.parent_span_id,
                payload=OTelSpanEvent.make(type_name, prio, attrs),
                timestamp=t_ns / 1e9,
            )
        )

    def _emit_start(span, step, engine, priority):
        start_attrs = _identity_attributes(span)
        if capture_payloads:
            input_preview = _preview(
                span.attributes, _INPUT_PREVIEW_KEYS, _INPUT_INDEXED
            )
            if input_preview is not None:
                start_attrs["input"] = _truncate(input_preview)
        _append(span, f"{step}.start", priority, start_attrs, engine, span.start_ns)

    def _emit_end(span, step, engine, priority):
        if span.is_error:
            error_attrs = _identity_attributes(span)
            error_type = span.attributes.get("error.type")
            if error_type is not None:
                error_attrs["error_type"] = str(error_type)
            if span.status_message:
                error_attrs["message"] = _truncate(span.status_message)
            _append(span, f"{step}.error", TracePriority.CRITICAL, error_attrs, engine, span.end_ns)
        else:
            _append(
                span,
                f"{step}.end",
                priority,
                _result_attributes(span, capture_payloads),
                engine,
                span.end_ns,
            )

    visited: set = set()
    # Stack entries: (span, is_closing). Push a span's close marker before its children
    # so it fires after them; reverse children so the first child is processed first.
    stack: List[Tuple[_OTelSpan, bool]] = [(root, False) for root in reversed(roots)]
    while stack:
        span, closing = stack.pop()
        kind = _classify(span)
        include = kind is not None or include_unclassified
        if not include:
            continue
        step = kind or UNCLASSIFIED
        engine = _engine_for(span, step) if kind is not None else (span.name or step)
        priority = (
            TracePriority.STRUCTURAL if kind is not None else TracePriority.TELEMETRY
        )
        if closing:
            _emit_end(span, step, engine, priority)
            continue
        if span.span_id in visited:
            continue  # cycle / re-entry guard
        visited.add(span.span_id)
        _emit_start(span, step, engine, priority)
        stack.append((span, True))
        for child in reversed(span.children):
            stack.append((child, False))


def ingest_otlp(
    source: Union[str, Mapping[str, Any], Sequence[Mapping[str, Any]]],
    store: Any,
    *,
    context_id: Optional[str] = None,
    include_unclassified: bool = False,
    capture_payloads: bool = True,
    schema_version: int = 1,
    skip_run_ids: Optional[FrozenSet[uuid.UUID]] = None,
) -> List[IngestedRun]:
    """Ingest OTLP/HTTP JSON traces into ``store``; one run per OTel trace.

    Args:
        source: a path to an OTLP JSON file (single request object, array, or
            JSON-lines), or an already-parsed request dict / list of request dicts.
        store: any :class:`~dprovenancekit.store.TraceStore` (typically
            :class:`~dprovenancekit.sqlite_store.SQLiteTraceStore`).
        context_id: override the run's context id (default: the root span's name, else
            the resource's ``service.name``, else the trace id).
        include_unclassified: keep non-GenAI spans as ``span.*`` telemetry events.
        capture_payloads: include truncated input/output previews in event attributes.
        schema_version: the envelope schema version stamped on ingested events.
        skip_run_ids: run ids to skip (e.g. traces already present in the database).

    Returns:
        One :class:`IngestedRun` per ingested trace, in file order. Traces whose derived
        run id is in ``skip_run_ids``, and traces yielding zero events (e.g. all spans
        unclassified with ``include_unclassified=False``), are omitted.

    Raises:
        ValueError: unreadable/invalid JSON input.
    """
    if isinstance(source, str):
        try:
            with open(source, "r", encoding="utf-8") as fh:
                requests = _parse_otlp_text(fh.read())
        except OSError as exc:
            raise ValueError(f"cannot read {source!r}: {exc}") from exc
    elif isinstance(source, Mapping):
        requests = [source]
    else:
        requests = [obj for obj in source if isinstance(obj, Mapping)]

    spans: List[_OTelSpan] = []
    for request in requests:
        spans.extend(_decode_spans(request))

    by_trace: Dict[str, List[_OTelSpan]] = {}
    trace_order: List[str] = []
    for span in spans:
        if span.trace_id not in by_trace:
            trace_order.append(span.trace_id)
        by_trace.setdefault(span.trace_id, []).append(span)

    ingested: List[IngestedRun] = []
    for trace_id in trace_order:
        run_id = run_id_for_trace(trace_id)
        if skip_run_ids and run_id in skip_run_ids:
            continue
        roots = _build_forest(by_trace[trace_id])
        run_context = context_id or next(
            (root.name for root in roots if root.name),
            None,
        )
        if run_context is None:
            service = next(
                (
                    str(root.resource["service.name"])
                    for root in roots
                    if root.resource.get("service.name")
                ),
                None,
            )
            run_context = service or trace_id

        events: List[TraceEvent] = []
        _emit_forest_events(
            roots,
            run_id=run_id,
            context_id=run_context,
            include_unclassified=include_unclassified,
            capture_payloads=capture_payloads,
            schema_version=schema_version,
            sink=events,
        )
        if not events:
            continue
        for event in events:
            store.record(event)
        ingested.append(
            IngestedRun(
                run_id=run_id,
                trace_id=trace_id,
                context_id=run_context,
                event_count=len(events),
            )
        )
    if ingested:
        store.flush()
    return ingested


__all__ = [
    "IngestedRun",
    "OTelSpanEvent",
    "ingest_otlp",
    "run_id_for_trace",
]

"""OpenAI Agents SDK integration — turn an agent run into a trace.

The OpenAI Agents SDK (the ``openai-agents`` package, imported as ``agents``) emits
structured tracing: each run is a *trace* containing nested *spans* (agent, generation,
function/tool call, response, handoff, guardrail, …). A
:class:`~agents.tracing.processor_interface.TracingProcessor` receives ``on_trace_start``
/ ``on_span_start`` / ``on_span_end`` / ``on_trace_end`` callbacks, each carrying
``trace_id`` / ``span_id`` / ``parent_id``. :class:`DProvenanceTracingProcessor` translates
that stream into DProvenanceKit runs:

* each trace becomes a run (``context_id`` = the trace name);
* each span start/end becomes a typed :class:`OpenAIAgentsTraceEvent` recorded in order —
  ``"<spanType>.start"`` / ``".end"`` / ``".error"`` (e.g. ``generation.start``,
  ``function.end``, ``guardrail.error``);
* the span's ``span_id`` / ``parent_id`` become the trace's **span tree**;
* the active component — agent name, tool name, model — becomes the **engine**;
* with ``link_lifecycle`` (default on), each completion is ``DERIVED_FROM`` its start and
  each child span is ``INFORMED`` by its parent.

Because everything flows through the normal recording path, the whole toolkit applies:
query the run, diff two runs, compare run **fingerprints** to detect a structurally
different agent path, or align two runs to grade a regression.

A processor is global, so one instance captures every trace produced while it is
registered. Register it once::

    from dprovenancekit import SQLiteTraceStore
    from dprovenancekit.integrations.openai_agents import (
        DProvenanceTracingProcessor, OpenAIAgentsTraceEvent, register,
    )

    store = SQLiteTraceStore(OpenAIAgentsTraceEvent, "traces.sqlite")
    register(store)              # add_trace_processor under the hood

    # ... run your agents normally; each run is recorded ...

Only registering (``register``) needs ``langchain``'s analogue ``openai-agents`` installed
(``pip install dprovenancekit[openai-agents]``). The translation logic imports nothing from
``agents``, so it can be unit-tested by driving the callbacks directly.
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional

from ..context import TraceContext
from ..edge import TraceEdgeType
from ..event import TraceableEvent
from ..kit import ActiveTraceRun
from ..priority import TracePriority

# Subclass the SDK's processor when installed so we are a first-class processor; fall back
# to ``object`` otherwise so the translation logic stays importable and testable without
# the dependency. The callback methods are identical either way.
try:  # pragma: no cover - import side-effect, exercised across envs
    from agents.tracing.processor_interface import TracingProcessor as _TracingProcessor

    _HAS_AGENTS = True
except Exception:  # noqa: BLE001
    _TracingProcessor = object  # type: ignore[assignment,misc]
    _HAS_AGENTS = False


# ── Event type ─────────────────────────────────────────────────────────────────


def _jsonable(obj: Any) -> Any:
    return str(obj)


@dataclass(frozen=True)
class OpenAIAgentsTraceEvent(TraceableEvent):
    """An OpenAI Agents SDK span lifecycle event.

    Parallel to ``integrations.langchain.LangChainTraceEvent``: attributes are stored as a
    canonical (sorted-key) JSON string so the event is hashable and two events with the
    same logical attributes compare equal (which makes exact-equality alignment work).
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
    ) -> "OpenAIAgentsTraceEvent":
        clean = {k: v for k, v in (attributes or {}).items() if v is not None}
        return cls(
            type_name=type_name,
            priority_value=int(priority),
            attributes_json=json.dumps(clean, sort_keys=True, default=_jsonable),
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
    def from_dict(cls, data: dict) -> "OpenAIAgentsTraceEvent":
        attrs = {k: v for k, v in data.items() if k not in ("type", "priority")}
        return cls.make(
            type_name=data["type"],
            priority=TracePriority(int(data.get("priority", int(TracePriority.STRUCTURAL)))),
            attributes=attrs,
        )


# ── Extraction helpers (defensive: span_data fields vary by type and SDK version) ─


def _truncate(text: str, limit: int = 2000) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def _names(items: Any) -> Optional[List[str]]:
    """Best-effort list of names from a list of tools/handoffs (strings or objects)."""
    if not isinstance(items, (list, tuple)):
        return None
    out: List[str] = []
    for it in items:
        name = getattr(it, "name", None)
        out.append(str(name) if name is not None else str(it))
    return out


def _usage_attrs(usage: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if isinstance(usage, Mapping):
        for key in ("input_tokens", "output_tokens", "total_tokens"):
            if key in usage:
                out[key] = usage[key]
    else:
        for key in ("input_tokens", "output_tokens", "total_tokens"):
            val = getattr(usage, key, None)
            if val is not None:
                out[key] = val
    return out


def _engine_for(span_data: Any, default: str) -> str:
    kind = getattr(span_data, "type", None)
    if kind in ("agent", "function", "guardrail", "custom", "task") and getattr(span_data, "name", None):
        return str(span_data.name)
    if kind in ("generation", "speech", "transcription") and getattr(span_data, "model", None):
        return str(span_data.model)
    if kind == "handoff":
        to_agent = getattr(span_data, "to_agent", None)
        if to_agent:
            return str(to_agent)
    return default


def _span_attributes(span_data: Any, capture_payloads: bool) -> Dict[str, Any]:
    """Pull a small, JSON-safe set of attributes off a span's data object."""
    kind = getattr(span_data, "type", "span")
    attrs: Dict[str, Any] = {}

    name = getattr(span_data, "name", None)
    if name is not None:
        attrs["name"] = str(name)

    if kind == "agent":
        attrs["tools"] = _names(getattr(span_data, "tools", None))
        attrs["handoffs"] = _names(getattr(span_data, "handoffs", None))
        output_type = getattr(span_data, "output_type", None)
        if output_type is not None:
            attrs["output_type"] = str(output_type)
    elif kind == "generation":
        model = getattr(span_data, "model", None)
        if model is not None:
            attrs["model"] = str(model)
        attrs.update(_usage_attrs(getattr(span_data, "usage", None)))
        if capture_payloads:
            output = getattr(span_data, "output", None)
            if output is not None:
                attrs["output"] = _truncate(str(output))
    elif kind == "response":
        attrs.update(_usage_attrs(getattr(span_data, "usage", None)))
        response = getattr(span_data, "response", None)
        response_id = getattr(response, "id", None)
        if response_id is not None:
            attrs["response_id"] = str(response_id)
    elif kind == "function":
        if capture_payloads:
            for field in ("input", "output"):
                val = getattr(span_data, field, None)
                if val is not None:
                    attrs[field] = _truncate(str(val))
    elif kind == "handoff":
        for field in ("from_agent", "to_agent"):
            val = getattr(span_data, field, None)
            if val is not None:
                attrs[field] = str(val)
    elif kind == "guardrail":
        triggered = getattr(span_data, "triggered", None)
        if triggered is not None:
            attrs["triggered"] = bool(triggered)
    elif kind == "custom":
        data = getattr(span_data, "data", None)
        if isinstance(data, Mapping):
            attrs["data_keys"] = sorted(str(k) for k in data.keys())

    return attrs


def _error_attributes(error: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if isinstance(error, Mapping):
        if error.get("message") is not None:
            out["message"] = _truncate(str(error["message"]))
        data = error.get("data")
        if isinstance(data, Mapping):
            out["data_keys"] = sorted(str(k) for k in data.keys())
    else:
        message = getattr(error, "message", None)
        if message is not None:
            out["message"] = _truncate(str(message))
    return out


# ── The processor ────────────────────────────────────────────────────────────────


class DProvenanceTracingProcessor(_TracingProcessor):  # type: ignore[misc,valid-type]
    """An OpenAI Agents SDK ``TracingProcessor`` that records DProvenanceKit runs.

    One instance handles many concurrent traces; it is safe to share across threads. Each
    trace opens a run keyed by ``trace_id`` and is flushed on ``on_trace_end``.

    Options:
        capture_payloads: include tool/generation IO previews in event attributes (else
            only structural metadata — names, models, token counts).
        link_lifecycle: emit provenance edges (``DERIVED_FROM`` start→end, ``INFORMED``
            parent→child).
    """

    def __init__(
        self,
        store: Any,
        *,
        schema_version: int = 1,
        capture_payloads: bool = True,
        link_lifecycle: bool = True,
    ) -> None:
        self._store = store
        self._schema_version = schema_version
        self._capture = capture_payloads
        self._link = link_lifecycle
        self._lock = threading.Lock()
        self._runs: Dict[str, ActiveTraceRun] = {}  # trace_id -> run
        self._start_event: Dict[str, uuid.UUID] = {}  # span_id -> start event id

    # MARK: - Trace lifecycle ----------------------------------------------------

    def on_trace_start(self, trace: Any) -> None:
        trace_id = getattr(trace, "trace_id", None)
        context_id = getattr(trace, "name", None) or str(trace_id)
        run = ActiveTraceRun(
            context_id=str(context_id),
            store=self._store,
            event_type=OpenAIAgentsTraceEvent,
            schema_version=self._schema_version,
        )
        with self._lock:
            self._runs[str(trace_id)] = run

    def on_trace_end(self, trace: Any) -> None:
        trace_id = str(getattr(trace, "trace_id", None))
        with self._lock:
            run = self._runs.pop(trace_id, None)
        if run is not None:
            run.flush()

    # MARK: - Span lifecycle -----------------------------------------------------

    def on_span_start(self, span: Any) -> None:
        with self._lock:
            run = self._runs.get(str(getattr(span, "trace_id", None)))
            if run is None:
                return
            span_data = getattr(span, "span_data", None)
            kind = getattr(span_data, "type", "span")
            event_id = self._record(
                run,
                f"{kind}.start",
                TracePriority.STRUCTURAL,
                _span_attributes(span_data, self._capture),
                engine=_engine_for(span_data, kind),
                span_id=getattr(span, "span_id", None),
                parent_id=getattr(span, "parent_id", None),
            )
            span_id = getattr(span, "span_id", None)
            if span_id is not None:
                self._start_event[str(span_id)] = event_id
            parent_id = getattr(span, "parent_id", None)
            if self._link and parent_id is not None:
                parent_start = self._start_event.get(str(parent_id))
                if parent_start is not None:
                    run.link(parent_start, event_id, TraceEdgeType.INFORMED)

    def on_span_end(self, span: Any) -> None:
        with self._lock:
            run = self._runs.get(str(getattr(span, "trace_id", None)))
            if run is None:
                return
            span_data = getattr(span, "span_data", None)
            kind = getattr(span_data, "type", "span")
            error = getattr(span, "error", None)
            if error is not None:
                type_name, priority, attrs = f"{kind}.error", TracePriority.CRITICAL, _error_attributes(error)
            else:
                attrs = _span_attributes(span_data, self._capture)
                # A triggered guardrail is a decision boundary worth never dropping.
                triggered = attrs.get("triggered") is True
                priority = TracePriority.CRITICAL if triggered else TracePriority.STRUCTURAL
                type_name = f"{kind}.end"
            event_id = self._record(
                run,
                type_name,
                priority,
                attrs,
                engine=_engine_for(span_data, kind),
                span_id=getattr(span, "span_id", None),
                parent_id=getattr(span, "parent_id", None),
            )
            span_id = getattr(span, "span_id", None)
            if self._link and span_id is not None:
                start_id = self._start_event.pop(str(span_id), None)
                if start_id is not None:
                    run.link(start_id, event_id, TraceEdgeType.DERIVED_FROM)

    # MARK: - Flush / shutdown ---------------------------------------------------

    def force_flush(self) -> None:
        with self._lock:
            runs = list(self._runs.values())
        for run in runs:
            run.flush()

    def shutdown(self) -> None:
        self.force_flush()

    # MARK: - Internal -----------------------------------------------------------

    def _record(
        self,
        run: ActiveTraceRun,
        type_name: str,
        priority: TracePriority,
        attributes: Mapping[str, Any],
        *,
        engine: Optional[str],
        span_id: Any,
        parent_id: Any,
    ) -> uuid.UUID:
        payload = OpenAIAgentsTraceEvent.make(type_name, priority, attributes)
        span = str(span_id) if span_id is not None else None
        parent = str(parent_id) if parent_id is not None else None
        span_token = TraceContext.current_span_id.set(span)
        parent_token = TraceContext.parent_span_id.set(parent)
        try:
            return run.record(payload, engine)
        finally:
            TraceContext.parent_span_id.reset(parent_token)
            TraceContext.current_span_id.reset(span_token)


def register(
    store: Any,
    *,
    schema_version: int = 1,
    capture_payloads: bool = True,
    link_lifecycle: bool = True,
) -> DProvenanceTracingProcessor:
    """Build a processor and register it with the Agents SDK (``add_trace_processor``).

    Requires ``openai-agents``. Returns the processor so callers can flush it or remove it
    later. Every trace produced while it is registered is recorded into ``store``.
    """
    from agents import add_trace_processor  # requires openai-agents

    processor = DProvenanceTracingProcessor(
        store,
        schema_version=schema_version,
        capture_payloads=capture_payloads,
        link_lifecycle=link_lifecycle,
    )
    add_trace_processor(processor)
    return processor


__all__ = [
    "DProvenanceTracingProcessor",
    "OpenAIAgentsTraceEvent",
    "register",
]

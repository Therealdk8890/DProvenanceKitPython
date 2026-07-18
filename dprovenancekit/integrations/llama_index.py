"""LlamaIndex integration — turn a LlamaIndex query or chat engine run into a trace.

LlamaIndex dispatches events through ``BaseCallbackHandler``: each operation (query,
retrieve, LLM call, synthesize, …) produces an ``on_event_start`` / ``on_event_end``
pair carrying LlamaIndex's own ``event_id`` / ``parent_id`` correlation.
:class:`DProvenanceLlamaIndexCallbackHandler` translates that stream into
DProvenanceKit events:

* each operation becomes one **span**: its start and end share the span, and the span
  nests under the parent operation's span using the ``parent_id`` LlamaIndex supplies —
  never an internal stack — so the recorded tree is correct even when operations
  overlap (parallel retrievers, async query engines) or end out of order;
* the active component — the model for LLM/embedding calls, the tool for tool calls,
  otherwise the ``CBEventType`` value (``query``, ``retrieve``, ``llm``, …) — becomes
  the **engine**, so diff signatures distinguish components;
* an end whose payload carries ``EventPayload.EXCEPTION`` is recorded as
  ``"<type>Errored"`` at :attr:`TracePriority.CRITICAL` (never dropped by budget),
  matching the other adapters' error convention;
* with ``link_lifecycle`` (default on), each end is ``DERIVED_FROM`` its start.

Payload capture follows the house convention (``capture_payloads``, default on): every
captured value is stringified and truncated to a bounded length, and bulky node/chunk
lists are reduced to counts, so a trace can never swallow a document set wholesale.
With capture off, only structural metadata (event ids, node counts, error summaries)
is recorded.

The handler is safe to share across threads: a single lock guards the open-event map,
mirroring the OpenAI Agents and CrewAI adapters. Only *registering* the handler with
LlamaIndex needs ``llama-index-core`` installed
(``pip install dprovenancekit[llama-index]``); the translation logic imports nothing
from ``llama_index``, so it can be unit-tested by driving the callbacks directly.
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

# Subclass LlamaIndex's handler when installed so we are a first-class callback handler;
# fall back to ``object`` otherwise so the translation logic stays importable and
# testable without the dependency. The callback methods are identical either way.
try:  # pragma: no cover - import side-effect, exercised across envs
    from llama_index.core.callbacks.base_handler import BaseCallbackHandler
    from llama_index.core.callbacks.schema import CBEventType

    _HAS_LLAMA_INDEX = True
except ImportError:
    BaseCallbackHandler = object  # type: ignore[assignment,misc]
    _HAS_LLAMA_INDEX = False


# ── Event type ─────────────────────────────────────────────────────────────────


def _jsonable(obj: Any) -> Any:
    return str(obj)


@dataclass(frozen=True)
class LlamaIndexTraceEvent(TraceableEvent):
    type_name: str
    priority_value: int
    attributes_json: str = "{}"

    @classmethod
    def make(
        cls,
        type_name: str,
        priority: TracePriority,
        attributes: Optional[Mapping[str, Any]] = None,
    ) -> "LlamaIndexTraceEvent":
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
    def from_dict(cls, data: dict) -> "LlamaIndexTraceEvent":
        attrs = {k: v for k, v in data.items() if k not in ("type", "priority")}
        return cls.make(
            type_name=data["type"],
            priority=TracePriority(
                int(data.get("priority", int(TracePriority.STRUCTURAL)))
            ),
            attributes=attrs,
        )


# ── Extraction helpers (defensive: payload keys are EventPayload str-enums) ──────


def _truncate(text: str, limit: int = 2000) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def _kind(event_type: Any) -> str:
    """The event type's plain name — ``CBEventType.LLM`` → ``"llm"``."""
    value = getattr(event_type, "value", None)
    return str(value) if value is not None else str(event_type)


def _key(key: Any) -> str:
    """A payload key's plain name (``EventPayload`` members are str-valued enums)."""
    value = getattr(key, "value", None)
    return str(value) if value is not None else str(key)


# Bulky payload lists reduced to counts instead of being captured or dropped.
_COUNTED_KEYS = {"nodes": "node_count", "chunks": "chunk_count"}

_REDACTED = "***redacted***"

# Key-name fragments that mark a value as a secret. Deliberately precise — bare "token"
# is excluded so token-count telemetry (prompt_tokens/total_tokens) is still captured.
_SENSITIVE_KEY_FRAGMENTS = (
    "api_key",
    "apikey",
    "secret",
    "password",
    "passwd",
    "authorization",
    "credential",
    "bearer",
    "private_key",
    "access_token",
    "refresh_token",
    "auth_token",
    "session_token",
)


def _is_sensitive_key(key: Any) -> bool:
    normalized = str(key).lower().replace("-", "_")
    return any(fragment in normalized for fragment in _SENSITIVE_KEY_FRAGMENTS)


def _redact_secrets(value: Any) -> Any:
    """Recursively replace secret-keyed values with a placeholder.

    LlamaIndex's ``EventPayload.SERIALIZED`` is a nested LLM/embedding config dict that has
    shipped an ``api_key`` in some versions; stringifying it verbatim leaked the key into
    the trace store (which this toolkit encourages committing as a golden baseline). This
    scrubs sensitive sub-keys while preserving useful structure like the model name.
    """
    if isinstance(value, Mapping):
        return {
            _key(k): (_REDACTED if _is_sensitive_key(k) else _redact_secrets(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_secrets(v) for v in value]
    return value


def _payload_attributes(
    payload: Optional[Mapping[Any, Any]], capture: bool
) -> Dict[str, Any]:
    """A bounded, JSON-safe attribute dict from a LlamaIndex event payload.

    Node/chunk lists become counts (always — they are structural metadata); every other
    value is captured only when ``capture`` is on, stringified and truncated so no
    single attribute can exceed the truncation limit, with secret-keyed values (including
    those nested inside the serialized config) redacted. The exception payload is handled
    by the error path, never here.
    """
    attrs: Dict[str, Any] = {}
    if not payload:
        return attrs
    for k, v in payload.items():
        key = _key(k)
        if key == "exception":
            continue
        counted = _COUNTED_KEYS.get(key)
        if counted is not None and isinstance(v, (list, tuple)):
            attrs[counted] = len(v)
        elif capture:
            if _is_sensitive_key(key):
                attrs[key] = _REDACTED
            else:
                attrs[key] = _truncate(str(_redact_secrets(v)))
    return attrs


def _exception_from(payload: Optional[Mapping[Any, Any]]) -> Optional[Any]:
    """The ``EventPayload.EXCEPTION`` value, if the payload carries one."""
    if not payload:
        return None
    for k, v in payload.items():
        if _key(k) == "exception" and v is not None:
            return v
    return None


def _engine_for(event_type: Any, payload: Optional[Mapping[Any, Any]]) -> str:
    """The active component's name — the trace's engine for this event.

    The model (from ``EventPayload.SERIALIZED``) for LLM/embedding calls, the tool name
    (from ``EventPayload.TOOL``) for tool calls, otherwise the ``CBEventType`` value —
    so diff signatures distinguish a retrieval step from the gpt-4o call it feeds.
    """
    if payload:
        for k, v in payload.items():
            key = _key(k)
            if key == "serialized" and isinstance(v, Mapping):
                model = v.get("model") or v.get("model_name")
                if model:
                    return str(model)
            elif key == "tool":
                name = getattr(v, "name", None)
                if name:
                    return str(name)
    return _kind(event_type)


# ── Per-operation bookkeeping ────────────────────────────────────────────────────


@dataclass(frozen=True)
class _OpenEvent:
    """Span identity, engine, and start-event id of a LlamaIndex event awaiting its
    end callback. Keyed by LlamaIndex's ``event_id``, so the end resolves to the same
    span and engine as its start regardless of arrival order."""

    span_id: str
    parent_span_id: Optional[str]
    start_event_id: uuid.UUID
    engine: str


# ── The handler ──────────────────────────────────────────────────────────────────


class DProvenanceLlamaIndexCallbackHandler(BaseCallbackHandler):
    """LlamaIndex callback handler that pushes events into an ActiveTraceRun.

    Lineage comes from LlamaIndex's own ``event_id`` / ``parent_id`` correlation — the
    framework assigns them at emission, so the recorded span tree is correct under
    concurrent and async execution where callback arrival order proves nothing. An
    event whose ``parent_id`` is unknown (LlamaIndex's root marker, or a parent this
    handler never saw) nests under the ambient span instead, so a query fired inside a
    ``@traced`` step still lands in the right place.

    The handler is safe to share across threads: a single lock guards all shared state.

    Options:
        link_lifecycle: emit a ``DERIVED_FROM`` edge from each start to its end.
        capture_payloads: include payload values (queries, prompts, responses) in event
            attributes, truncated. With it off, only structural metadata is kept
            (event ids, node/chunk counts, error summaries).
    """

    def __init__(
        self,
        trace_run: ActiveTraceRun,
        link_lifecycle: bool = True,
        *,
        capture_payloads: bool = True,
    ):
        if _HAS_LLAMA_INDEX:
            super().__init__(event_starts_to_ignore=[], event_ends_to_ignore=[])
        self.trace_run = trace_run
        self.link_lifecycle = link_lifecycle
        self.capture_payloads = capture_payloads
        self._lock = threading.Lock()
        self._open_events: Dict[str, _OpenEvent] = {}  # llama event_id -> open span

    def _record_in_span(
        self,
        event: LlamaIndexTraceEvent,
        engine: str,
        span_id: str,
        parent_span_id: Optional[str],
    ) -> uuid.UUID:
        """Record under an explicit span, set transiently and reset immediately
        (mirrors ``dprovenancekit.instrument._record_in_span``)."""
        span_token = TraceContext.current_span_id.set(span_id)
        parent_token = TraceContext.parent_span_id.set(parent_span_id)
        try:
            return self.trace_run.record(event, engine_name=engine)
        finally:
            TraceContext.parent_span_id.reset(parent_token)
            TraceContext.current_span_id.reset(span_token)

    def on_event_start(
        self,
        event_type: "CBEventType",
        payload: Optional[Dict[str, Any]] = None,
        event_id: str = "",
        parent_id: str = "",
        **kwargs: Any,
    ) -> str:
        span_id = str(uuid.uuid4())
        engine = _engine_for(event_type, payload)
        attrs: Dict[str, Any] = {"llama_event_id": event_id}
        attrs.update(_payload_attributes(payload, self.capture_payloads))
        event = LlamaIndexTraceEvent.make(
            type_name=f"{_kind(event_type)}Started",
            priority=TracePriority.STRUCTURAL,
            attributes=attrs,
        )

        with self._lock:
            # Nest under the span of the parent LlamaIndex hands us — its parent_id is
            # assigned when the event is emitted, so it is authoritative even when
            # sibling operations run concurrently. Unknown parent (LlamaIndex's "root",
            # or one we never saw): fall back to the ambient span, so the handler
            # composes with instrumented code (e.g. a @traced step).
            parent = self._open_events.get(parent_id) if parent_id else None
            parent_span_id = (
                parent.span_id
                if parent is not None
                else TraceContext.current_span_id.get()
            )
            start_event_id = self._record_in_span(event, engine, span_id, parent_span_id)
            if event_id:
                self._open_events[event_id] = _OpenEvent(
                    span_id=span_id,
                    parent_span_id=parent_span_id,
                    start_event_id=start_event_id,
                    engine=engine,
                )
        return event_id

    def on_event_end(
        self,
        event_type: "CBEventType",
        payload: Optional[Dict[str, Any]] = None,
        event_id: str = "",
        **kwargs: Any,
    ) -> None:
        exception = _exception_from(payload)
        attrs: Dict[str, Any] = {"llama_event_id": event_id}
        if exception is not None:
            # A failed operation is a decision boundary worth never dropping.
            type_name = f"{_kind(event_type)}Errored"
            priority = TracePriority.CRITICAL
            attrs["error_type"] = type(exception).__name__
            attrs["message"] = _truncate(str(exception))
        else:
            type_name = f"{_kind(event_type)}Ended"
            priority = TracePriority.STRUCTURAL
            attrs.update(_payload_attributes(payload, self.capture_payloads))

        with self._lock:
            open_event = self._open_events.pop(event_id, None) if event_id else None
            if open_event is not None:
                # The end shares the start's span and engine (same model as
                # instrument.traced: one span brackets a step's whole lifecycle).
                span_id = open_event.span_id
                parent_span_id = open_event.parent_span_id
                engine = open_event.engine
            else:
                # End without a recorded start: record it in its own span rather than
                # dropping it.
                span_id = str(uuid.uuid4())
                parent_span_id = TraceContext.current_span_id.get()
                engine = _engine_for(event_type, payload)

            event = LlamaIndexTraceEvent.make(
                type_name=type_name, priority=priority, attributes=attrs
            )
            end_event_id = self._record_in_span(event, engine, span_id, parent_span_id)

            if self.link_lifecycle and open_event is not None:
                self.trace_run.link(
                    open_event.start_event_id,
                    end_event_id,
                    TraceEdgeType.DERIVED_FROM,
                )

    def start_trace(self, trace_id: Optional[str] = None) -> None:
        pass

    def end_trace(
        self,
        trace_id: Optional[str] = None,
        trace_map: Optional[Dict[str, List[str]]] = None,
    ) -> None:
        pass


__all__ = [
    "DProvenanceLlamaIndexCallbackHandler",
    "LlamaIndexTraceEvent",
]

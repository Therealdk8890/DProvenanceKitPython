"""LlamaIndex integration — turn a LlamaIndex query or chat engine run into a trace.

LlamaIndex dispatches events through `BaseCallbackHandler`. We capture events like
LLM calls, chunk retrieval, and tool usage, mapping them to DProvenanceKit events.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional

from ..context import TraceContext
from ..edge import TraceEdgeType
from ..event import TraceableEvent
from ..kit import ActiveTraceRun
from ..priority import TracePriority

try:
    from llama_index.core.callbacks.base_handler import BaseCallbackHandler
    from llama_index.core.callbacks.schema import CBEventType

    _HAS_LLAMA_INDEX = True
except ImportError:
    BaseCallbackHandler = object  # type: ignore[assignment,misc]
    _HAS_LLAMA_INDEX = False


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


@dataclass(frozen=True)
class _OpenEvent:
    """Span identity and start-event id of a LlamaIndex event awaiting its end callback."""

    span_id: str
    parent_span_id: Optional[str]
    start_event_id: uuid.UUID


class DProvenanceLlamaIndexCallbackHandler(BaseCallbackHandler):
    """LlamaIndex callback handler that pushes events into an ActiveTraceRun."""

    def __init__(self, trace_run: ActiveTraceRun, link_lifecycle: bool = True):
        super().__init__(event_starts_to_ignore=[], event_ends_to_ignore=[])
        self.trace_run = trace_run
        self.link_lifecycle = link_lifecycle
        self._span_stack: list[str] = []
        self._open_events: dict[str, _OpenEvent] = {}

    def _record_in_span(
        self,
        event: LlamaIndexTraceEvent,
        span_id: str,
        parent_span_id: Optional[str],
    ) -> uuid.UUID:
        """Record under an explicit span, set transiently and reset immediately
        (mirrors ``dprovenancekit.instrument._record_in_span``)."""
        span_token = TraceContext.current_span_id.set(span_id)
        parent_token = TraceContext.parent_span_id.set(parent_span_id)
        try:
            return self.trace_run.record(event, engine_name="llama_index")
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
        # Nest under the enclosing LlamaIndex event, or the ambient span if the
        # handler fires inside instrumented code (e.g. a @traced step).
        parent_span_id = (
            self._span_stack[-1]
            if self._span_stack
            else TraceContext.current_span_id.get()
        )

        attrs = {"llama_event_id": event_id}
        if payload:
            for k, v in payload.items():
                # Avoid storing full node texts or massive prompts by default to keep trace sizes down,
                # unless they are the direct queries.
                if k not in ["nodes", "chunks", "prompt"]:
                    attrs[k] = str(v)

        event = LlamaIndexTraceEvent.make(
            type_name=(
                f"{event_type.value}Started"
                if hasattr(event_type, "value")
                else f"{event_type}Started"
            ),
            priority=TracePriority.STRUCTURAL,
            attributes=attrs,
        )

        start_event_id = self._record_in_span(event, span_id, parent_span_id)
        self._open_events[event_id] = _OpenEvent(
            span_id=span_id,
            parent_span_id=parent_span_id,
            start_event_id=start_event_id,
        )
        self._span_stack.append(span_id)
        return event_id

    def on_event_end(
        self,
        event_type: "CBEventType",
        payload: Optional[Dict[str, Any]] = None,
        event_id: str = "",
        **kwargs: Any,
    ) -> None:
        open_event = self._open_events.pop(event_id, None)
        if open_event is not None:
            # The end event shares the start event's span (same model as
            # instrument.traced: one span brackets a step's whole lifecycle).
            span_id = open_event.span_id
            parent_span_id = open_event.parent_span_id
            if span_id in self._span_stack:
                self._span_stack.remove(span_id)
        else:
            # End without a recorded start: record it in its own span rather
            # than dropping it, but leave the stack untouched.
            span_id = str(uuid.uuid4())
            parent_span_id = (
                self._span_stack[-1]
                if self._span_stack
                else TraceContext.current_span_id.get()
            )

        attrs = {"llama_event_id": event_id}
        if payload:
            for k, v in payload.items():
                if k not in ["nodes", "chunks", "response"]:
                    attrs[k] = str(v)
                elif k == "response":
                    attrs["response_preview"] = str(v)[:500] + (
                        "..." if len(str(v)) > 500 else ""
                    )

        event = LlamaIndexTraceEvent.make(
            type_name=(
                f"{event_type.value}Ended"
                if hasattr(event_type, "value")
                else f"{event_type}Ended"
            ),
            priority=TracePriority.STRUCTURAL,
            attributes=attrs,
        )

        end_event_id = self._record_in_span(event, span_id, parent_span_id)

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

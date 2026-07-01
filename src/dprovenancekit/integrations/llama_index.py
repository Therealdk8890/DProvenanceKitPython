"""LlamaIndex integration — turn a LlamaIndex query or chat engine run into a trace.

LlamaIndex dispatches events through `BaseCallbackHandler`. We capture events like
LLM calls, chunk retrieval, and tool usage, mapping them to DProvenanceKit events.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, cast

from ..edge import TraceEdgeType
from ..event import TraceableEvent
from ..kit import ActiveTraceRun
from ..priority import TracePriority


try:
    from llama_index.core.callbacks.base_handler import BaseCallbackHandler
    from llama_index.core.callbacks.schema import CBEventType, EventPayload
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
            priority=TracePriority(int(data.get("priority", int(TracePriority.STRUCTURAL)))),
            attributes=attrs,
        )


class DProvenanceLlamaIndexCallbackHandler(BaseCallbackHandler):
    """LlamaIndex callback handler that pushes events into an ActiveTraceRun."""

    def __init__(self, trace_run: ActiveTraceRun, link_lifecycle: bool = True):
        super().__init__(
            event_starts_to_ignore=[],
            event_ends_to_ignore=[]
        )
        self.trace_run = trace_run
        self.link_lifecycle = link_lifecycle
        self._span_stack: list[uuid.UUID] = []
        self._event_spans: dict[str, uuid.UUID] = {}

    def on_event_start(
        self,
        event_type: "CBEventType",
        payload: Optional[Dict[str, Any]] = None,
        event_id: str = "",
        parent_id: str = "",
        **kwargs: Any,
    ) -> str:
        span_id = uuid.uuid4()
        self._event_spans[event_id] = span_id

        parent_span_id = self._span_stack[-1] if self._span_stack else None
        self._span_stack.append(span_id)

        attrs = {"llama_event_id": event_id}
        if payload:
            for k, v in payload.items():
                # Avoid storing full node texts or massive prompts by default to keep trace sizes down,
                # unless they are the direct queries.
                if k not in ["nodes", "chunks", "prompt"]: 
                    attrs[k] = str(v)

        event = LlamaIndexTraceEvent.make(
            type_name=f"{event_type.value}Started" if hasattr(event_type, "value") else f"{event_type}Started",
            priority=TracePriority.STRUCTURAL,
            attributes=attrs,
        )

        self.trace_run.append(
            event=event,
            engine_name="llama_index",
            span_id=span_id,
            parent_span_id=parent_span_id,
        )
        return event_id

    def on_event_end(
        self,
        event_type: "CBEventType",
        payload: Optional[Dict[str, Any]] = None,
        event_id: str = "",
        **kwargs: Any,
    ) -> None:
        if self._span_stack:
            self._span_stack.pop()

        start_span_id = self._event_spans.pop(event_id, None)
        end_span_id = uuid.uuid4()

        attrs = {"llama_event_id": event_id}
        if payload:
            for k, v in payload.items():
                if k not in ["nodes", "chunks", "response"]:
                    attrs[k] = str(v)
                elif k == "response":
                    attrs["response_preview"] = str(v)[:500] + ("..." if len(str(v)) > 500 else "")

        event = LlamaIndexTraceEvent.make(
            type_name=f"{event_type.value}Ended" if hasattr(event_type, "value") else f"{event_type}Ended",
            priority=TracePriority.STRUCTURAL,
            attributes=attrs,
        )

        self.trace_run.append(
            event=event,
            engine_name="llama_index",
            span_id=end_span_id,
        )
        
        if self.link_lifecycle and start_span_id:
            self.trace_run.link(
                source_span_id=end_span_id,
                target_span_id=start_span_id,
                edge_type=TraceEdgeType.DERIVED_FROM,
            )

    def start_trace(self, trace_id: Optional[str] = None) -> None:
        pass

    def end_trace(self, trace_id: Optional[str] = None, trace_map: Optional[Dict[str, List[str]]] = None) -> None:
        pass

# git-blob-rewrite

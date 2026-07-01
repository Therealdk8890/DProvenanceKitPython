"""Google GenAI integration — wrap the google-genai SDK client.

This provides a lightweight wrapper around the standard `google.genai.Client` to
automatically trace generation calls.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

from ..edge import TraceEdgeType
from ..event import TraceableEvent
from ..kit import ActiveTraceRun
from ..priority import TracePriority


def _jsonable(obj: Any) -> Any:
    return str(obj)


@dataclass(frozen=True)
class GoogleGenAITraceEvent(TraceableEvent):
    type_name: str
    priority_value: int
    attributes_json: str = "{}"

    @classmethod
    def make(
        cls,
        type_name: str,
        priority: TracePriority,
        attributes: Optional[Mapping[str, Any]] = None,
    ) -> "GoogleGenAITraceEvent":
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
    def from_dict(cls, data: dict) -> "GoogleGenAITraceEvent":
        attrs = {k: v for k, v in data.items() if k not in ("type", "priority")}
        return cls.make(
            type_name=data["type"],
            priority=TracePriority(int(data.get("priority", int(TracePriority.STRUCTURAL)))),
            attributes=attrs,
        )


class DProvenanceGenAIWrapper:
    """Wraps a Google GenAI client to capture traces for generate_content."""

    def __init__(self, client: Any, trace_run: ActiveTraceRun, link_lifecycle: bool = True):
        self.client = client
        self.trace_run = trace_run
        self.link_lifecycle = link_lifecycle
        self.models = _ModelsWrapper(self.client.models, self.trace_run, self.link_lifecycle)


class _ModelsWrapper:
    def __init__(self, models_client: Any, trace_run: ActiveTraceRun, link_lifecycle: bool):
        self._models_client = models_client
        self.trace_run = trace_run
        self.link_lifecycle = link_lifecycle

    def generate_content(self, *args, **kwargs):
        start_span = uuid.uuid4()
        model_name = kwargs.get("model") or (args[0] if args else "unknown")
        
        # Capture Start
        start_event = GoogleGenAITraceEvent.make(
            type_name="generateContentStarted",
            priority=TracePriority.STRUCTURAL,
            attributes={"model": model_name}
        )
        self.trace_run.append(
            event=start_event,
            engine_name="google_genai",
            span_id=start_span
        )

        try:
            response = self._models_client.generate_content(*args, **kwargs)
            
            end_span = uuid.uuid4()
            end_event = GoogleGenAITraceEvent.make(
                type_name="generateContentEnded",
                priority=TracePriority.STRUCTURAL,
                attributes={
                    "model": model_name,
                    "response_preview": response.text[:500] if hasattr(response, "text") else "..."
                }
            )
            self.trace_run.append(
                event=end_event,
                engine_name="google_genai",
                span_id=end_span
            )
            if self.link_lifecycle:
                self.trace_run.link(end_span, start_span, TraceEdgeType.DERIVED_FROM)
            
            return response

        except Exception as e:
            err_span = uuid.uuid4()
            err_event = GoogleGenAITraceEvent.make(
                type_name="generateContentError",
                priority=TracePriority.STRUCTURAL,
                attributes={"error": str(e)}
            )
            self.trace_run.append(
                event=err_event,
                engine_name="google_genai",
                span_id=err_span
            )
            if self.link_lifecycle:
                self.trace_run.link(err_span, start_span, TraceEdgeType.DERIVED_FROM)
            raise e

# git-blob-rewrite

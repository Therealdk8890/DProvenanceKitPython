"""Google GenAI integration — wrap the google-genai SDK client.

This provides a lightweight wrapper around the standard `google.genai.Client` to
automatically trace generation calls.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

from ..context import TraceContext
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
            priority=TracePriority(
                int(data.get("priority", int(TracePriority.STRUCTURAL)))
            ),
            attributes=attrs,
        )


class DProvenanceGenAIWrapper:
    """Wraps a Google GenAI client to capture traces for generate_content."""

    def __init__(
        self, client: Any, trace_run: ActiveTraceRun, link_lifecycle: bool = True
    ):
        self.client = client
        self.trace_run = trace_run
        self.link_lifecycle = link_lifecycle
        self.models = _ModelsWrapper(
            self.client.models, self.trace_run, self.link_lifecycle
        )


class _ModelsWrapper:
    def __init__(
        self, models_client: Any, trace_run: ActiveTraceRun, link_lifecycle: bool
    ):
        self._models_client = models_client
        self.trace_run = trace_run
        self.link_lifecycle = link_lifecycle

    def generate_content(self, *args, **kwargs):
        model_name = kwargs.get("model") or (args[0] if args else "unknown")
        # Start and end of one generate_content call share a single span so the
        # pair reads as one node in the span tree. `record()` reads the span from
        # the contextvar (there is no span_id kwarg), so set it for the call.
        call_span = str(uuid.uuid4())
        span_token = TraceContext.current_span_id.set(call_span)
        try:
            # Capture Start
            start_event = GoogleGenAITraceEvent.make(
                type_name="generateContentStarted",
                priority=TracePriority.STRUCTURAL,
                attributes={"model": model_name},
            )
            start_id = self.trace_run.record(start_event, "google_genai")

            try:
                response = self._models_client.generate_content(*args, **kwargs)
            except Exception as e:
                err_event = GoogleGenAITraceEvent.make(
                    type_name="generateContentError",
                    priority=TracePriority.STRUCTURAL,
                    attributes={"error": str(e)},
                )
                err_id = self.trace_run.record(err_event, "google_genai")
                if self.link_lifecycle:
                    self.trace_run.link(
                        start_id, err_id, TraceEdgeType.DERIVED_FROM
                    )
                raise

            end_event = GoogleGenAITraceEvent.make(
                type_name="generateContentEnded",
                priority=TracePriority.STRUCTURAL,
                attributes={
                    "model": model_name,
                    "response_preview": (
                        response.text[:500] if hasattr(response, "text") else "..."
                    ),
                },
            )
            end_id = self.trace_run.record(end_event, "google_genai")
            if self.link_lifecycle:
                self.trace_run.link(start_id, end_id, TraceEdgeType.DERIVED_FROM)

            return response
        finally:
            TraceContext.current_span_id.reset(span_token)

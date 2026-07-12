"""Google GenAI integration — wrap the ``google-genai`` client to trace generation calls.

:class:`DProvenanceGenAIWrapper` wraps a standard ``google.genai.Client`` so that
synchronous, non-streaming ``models.generate_content`` calls are recorded into an open
:class:`~dprovenancekit.kit.ActiveTraceRun`:

* each call records a ``generateContentStarted`` / ``generateContentEnded`` pair — or
  ``generateContentError`` at :attr:`~dprovenancekit.priority.TracePriority.CRITICAL`
  when the SDK raises — and the pair shares one span so it reads as a single node in
  the span tree;
* the request's **model name** becomes the **engine**, matching how the other adapters
  and the OTel ingester key LLM calls on the model identity;
* the response's text preview and token usage (``usage_metadata``) are extracted
  defensively: ``GenerateContentResponse.text`` is a *property* that can return ``None``
  or raise (e.g. on a safety-blocked or non-text response), and tracing must never turn
  a successful API call into a crash, so extraction failures collapse to a placeholder
  instead of propagating into user code;
* with ``link_lifecycle`` (default on), each completion (or error) is ``DERIVED_FROM``
  its start.

Every attribute the wrapper does not trace — ``models.generate_content_stream``,
``models.embed_content``, ``client.chats``, ``client.aio``, ``client.files``, … — is
delegated to the wrapped object via ``__getattr__``, so the wrapper is a drop-in for the
real client; untraced surfaces simply pass through untraced.

The module imports nothing from ``google.genai``, so it stays importable and
unit-testable without the dependency (``pip install dprovenancekit[google-genai]`` for
the real client)::

    from google import genai
    from dprovenancekit import DProvenanceKit, SQLiteTraceStore
    from dprovenancekit.integrations.google_genai import (
        DProvenanceGenAIWrapper, GoogleGenAITraceEvent,
    )

    kit = DProvenanceKit(GoogleGenAITraceEvent)
    store = SQLiteTraceStore(GoogleGenAITraceEvent, "traces.sqlite")
    with kit.run(context_id="my-app", store=store) as run:
        client = DProvenanceGenAIWrapper(genai.Client(), run)
        client.models.generate_content(model="gemini-2.0-flash", contents="hi")
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Tuple

from ..context import TraceContext
from ..edge import TraceEdgeType
from ..event import TraceableEvent
from ..kit import ActiveTraceRun
from ..priority import TracePriority

# ── Event type ─────────────────────────────────────────────────────────────────


def _jsonable(obj: Any) -> Any:
    return str(obj)


@dataclass(frozen=True)
class GoogleGenAITraceEvent(TraceableEvent):
    """A Google GenAI call lifecycle event.

    Parallel to ``integrations.openai_agents.OpenAIAgentsTraceEvent``: attributes are
    stored as a canonical (sorted-key) JSON string so the event is hashable and two
    events with the same logical attributes compare equal (which makes exact-equality
    alignment work).
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


# ── Extraction helpers (defensive: response fields are properties that can raise) ─


_TEXT_PLACEHOLDER = "<text unavailable>"


def _truncate(text: str, limit: int = 500) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def _safe_getattr(obj: Any, name: str) -> Any:
    """``getattr`` that treats a raising property as absent.

    The google-genai response object computes several attributes lazily in properties;
    a raise while *tracing* must never surface into user code.
    """
    try:
        return getattr(obj, name, None)
    except Exception:  # noqa: BLE001 - anything the property raises means "absent"
        return None


def _model_name(args: Tuple[Any, ...], kwargs: Mapping[str, Any]) -> str:
    """The request's model identity (``model=`` kwarg or first positional arg)."""
    model = kwargs.get("model")
    if model is None and args:
        model = args[0]
    return str(model) if model not in (None, "") else "unknown"


def _response_preview(response: Any) -> Optional[str]:
    """Best-effort text preview of a response.

    ``response.text`` is Optional and the property raises on some responses (e.g.
    safety-blocked). A raise collapses to a placeholder; ``None`` (a valid "no text
    parts" answer, e.g. a pure function-call response) records no preview at all.
    """
    try:
        text = response.text
    except Exception:  # noqa: BLE001 - blocked/invalid response; never raise from tracing
        return _TEXT_PLACEHOLDER
    if text is None:
        return None
    try:
        return _truncate(str(text))
    except Exception:  # noqa: BLE001
        return _TEXT_PLACEHOLDER


def _usage_attrs(usage: Any) -> Dict[str, Any]:
    """Pull token counts off a ``usage_metadata`` object/mapping (SDK field names)."""
    out: Dict[str, Any] = {}
    keys = ("prompt_token_count", "candidates_token_count", "total_token_count")
    if isinstance(usage, Mapping):
        for key in keys:
            if usage.get(key) is not None:
                out[key] = usage[key]
    elif usage is not None:
        for key in keys:
            val = _safe_getattr(usage, key)
            if val is not None:
                out[key] = val
    return out


def _response_attributes(model_name: str, response: Any) -> Dict[str, Any]:
    attrs: Dict[str, Any] = {"model": model_name}
    attrs["response_preview"] = _response_preview(response)  # None dropped by make()
    attrs.update(_usage_attrs(_safe_getattr(response, "usage_metadata")))
    return attrs


# ── The wrappers ─────────────────────────────────────────────────────────────────


class DProvenanceGenAIWrapper:
    """Wraps a Google GenAI client so ``models.generate_content`` calls are traced.

    ``wrapper.models`` is a :class:`_ModelsWrapper` tracing ``generate_content``; every
    other attribute (``chats``, ``aio``, ``files``, ``batches``, …) is delegated to the
    wrapped client unchanged, so the wrapper is a drop-in replacement.
    """

    def __init__(
        self, client: Any, trace_run: ActiveTraceRun, link_lifecycle: bool = True
    ):
        self.client = client
        self.trace_run = trace_run
        self.link_lifecycle = link_lifecycle
        self.models = _ModelsWrapper(
            self.client.models, self.trace_run, self.link_lifecycle
        )

    def __getattr__(self, name: str) -> Any:
        # Only reached for attributes not set on the wrapper itself.
        return getattr(self.client, name)


class _ModelsWrapper:
    """Traces ``generate_content``; delegates every other attribute to the real
    ``client.models`` so untraced surfaces (``generate_content_stream``,
    ``embed_content``, ``count_tokens``, …) keep working, just untraced."""

    def __init__(
        self, models_client: Any, trace_run: ActiveTraceRun, link_lifecycle: bool
    ):
        self._models_client = models_client
        self.trace_run = trace_run
        self.link_lifecycle = link_lifecycle

    def __getattr__(self, name: str) -> Any:
        # Only reached for attributes not set on the wrapper itself.
        return getattr(self._models_client, name)

    def generate_content(self, *args, **kwargs):
        model_name = _model_name(args, kwargs)
        # Start and end of one generate_content call share a single span so the
        # pair reads as one node in the span tree. `record()` reads the span from
        # the contextvar (there is no span_id kwarg), so set it for the call.
        call_span = str(uuid.uuid4())
        span_token = TraceContext.current_span_id.set(call_span)
        try:
            start_event = GoogleGenAITraceEvent.make(
                type_name="generateContentStarted",
                priority=TracePriority.STRUCTURAL,
                attributes={"model": model_name},
            )
            start_id = self.trace_run.record(start_event, model_name)

            try:
                response = self._models_client.generate_content(*args, **kwargs)
            except Exception as e:
                err_event = GoogleGenAITraceEvent.make(
                    type_name="generateContentError",
                    priority=TracePriority.CRITICAL,
                    attributes={"model": model_name, "error": _truncate(str(e))},
                )
                err_id = self.trace_run.record(err_event, model_name)
                if self.link_lifecycle:
                    self.trace_run.link(start_id, err_id, TraceEdgeType.DERIVED_FROM)
                raise

            end_event = GoogleGenAITraceEvent.make(
                type_name="generateContentEnded",
                priority=TracePriority.STRUCTURAL,
                attributes=_response_attributes(model_name, response),
            )
            end_id = self.trace_run.record(end_event, model_name)
            if self.link_lifecycle:
                self.trace_run.link(start_id, end_id, TraceEdgeType.DERIVED_FROM)

            return response
        finally:
            TraceContext.current_span_id.reset(span_token)


__all__ = [
    "DProvenanceGenAIWrapper",
    "GoogleGenAITraceEvent",
]

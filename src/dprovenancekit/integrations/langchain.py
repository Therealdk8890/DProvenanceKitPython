"""LangChain / LangGraph integration — turn a chain or graph run into a trace.

LangChain dispatches lifecycle callbacks (``on_llm_start``, ``on_tool_start``,
``on_chain_start`` …) through a :class:`~langchain_core.callbacks.base.BaseCallbackHandler`.
Every callback carries a ``run_id`` and ``parent_run_id`` identifying its node in
LangChain's own run tree. :class:`DProvenanceCallbackHandler` translates that stream
into DProvenanceKit events:

* each callback becomes a typed :class:`LangChainTraceEvent` recorded in commit order,
  so the per-run **sequence** mirrors true execution order;
* ``run_id`` → ``span_id`` and ``parent_run_id`` → ``parent_span_id``, so the trace's
  span tree is LangChain's run tree;
* the active component (model / tool / retriever / chain name) becomes the **engine**;
* with ``link_lifecycle`` (default on), each completion is linked ``DERIVED_FROM`` its
  start, and each child step ``INFORMED`` by its parent — a queryable provenance graph.

Because every event flows through the same recording path as hand-written events, all
of DProvenanceKit applies unchanged: query the run, diff two runs, compare run
**fingerprints** to detect when an agent took a structurally different path, or align
runs to grade a regression.

Typical use::

    from dprovenancekit import SQLiteTraceStore
    from dprovenancekit.integrations.langchain import DProvenanceTracer, LangChainTraceEvent

    store = SQLiteTraceStore(LangChainTraceEvent, "traces.sqlite")
    tracer = DProvenanceTracer(store)

    with tracer.trace(context_id="customer-42") as cb:
        result = chain.invoke(question, config={"callbacks": [cb]})

    # afterwards: query / diff / fingerprint the recorded run via `store`.

This module requires ``langchain-core`` only to be wired into a live chain (you need it
to have a chain at all). The translation logic itself imports nothing from LangChain, so
it can be unit-tested by driving the callbacks directly.
"""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence

from ..context import TraceContext
from ..edge import TraceEdgeType
from ..event import TraceableEvent
from ..kit import ActiveTraceRun
from ..priority import TracePriority

# Subclass LangChain's handler when it is installed so we are a first-class handler; fall
# back to ``object`` otherwise so the translation logic stays importable and testable
# without the dependency. The ``on_*`` methods below are identical either way.
try:  # pragma: no cover - import side-effect, both branches are exercised across envs
    from langchain_core.callbacks.base import (
        BaseCallbackHandler as _BaseCallbackHandler,
    )

    _HAS_LANGCHAIN = True
except Exception:  # noqa: BLE001 - any import failure means "not available"
    _BaseCallbackHandler = object  # type: ignore[assignment,misc]
    _HAS_LANGCHAIN = False


# ── Event type ─────────────────────────────────────────────────────────────────


class LCEventType:
    """Stable ``type_identifier`` constants for LangChain lifecycle events.

    These are the keys that querying, diffing, and fingerprinting are defined over, so
    they are treated as contract values — stable across versions of this adapter.
    """

    CHAIN_STARTED = "chainStarted"
    CHAIN_ENDED = "chainEnded"
    CHAIN_ERROR = "chainError"

    LLM_STARTED = "llmStarted"
    LLM_ENDED = "llmEnded"
    LLM_ERROR = "llmError"
    CHAT_MODEL_STARTED = "chatModelStarted"

    TOOL_STARTED = "toolStarted"
    TOOL_ENDED = "toolEnded"
    TOOL_ERROR = "toolError"

    RETRIEVER_STARTED = "retrieverStarted"
    RETRIEVER_ENDED = "retrieverEnded"
    RETRIEVER_ERROR = "retrieverError"

    AGENT_ACTION = "agentAction"
    AGENT_FINISH = "agentFinish"

    TEXT = "text"


def _jsonable(obj: Any) -> Any:
    """Last-resort coercion for json.dumps: render anything exotic as its string form."""
    return str(obj)


@dataclass(frozen=True)
class LangChainTraceEvent(TraceableEvent):
    """A LangChain lifecycle event.

    Attributes are stored as a canonical (sorted-key) JSON string rather than a live
    dict so the event is hashable and two events with the same logical attributes compare
    equal — which is what makes exact-equality alignment meaningful for adapter runs.
    Build instances with :meth:`make`; read attributes back via :attr:`attributes`.
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
    ) -> "LangChainTraceEvent":
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
    def from_dict(cls, data: dict) -> "LangChainTraceEvent":
        attrs = {k: v for k, v in data.items() if k not in ("type", "priority")}
        return cls.make(
            type_name=data["type"],
            priority=TracePriority(
                int(data.get("priority", int(TracePriority.STRUCTURAL)))
            ),
            attributes=attrs,
        )


# ── Extraction helpers (defensive: never assume an exact LangChain version) ─────


def _component_name(serialized: Any, kwargs: Mapping[str, Any], default: str) -> str:
    name = kwargs.get("name") if kwargs else None
    if name:
        return str(name)
    if isinstance(serialized, Mapping):
        if serialized.get("name"):
            return str(serialized["name"])
        ident = serialized.get("id")
        if isinstance(ident, (list, tuple)) and ident:
            return str(ident[-1])
    return default


def _keys(obj: Any) -> Optional[List[str]]:
    if isinstance(obj, Mapping):
        return sorted(str(k) for k in obj.keys())
    return None


def _truncate(text: str, limit: int = 2000) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def _llm_end_attributes(response: Any) -> Dict[str, Any]:
    attrs: Dict[str, Any] = {}
    generations = getattr(response, "generations", None)
    if isinstance(generations, (list, tuple)):
        attrs["generation_count"] = sum(
            len(group) for group in generations if isinstance(group, (list, tuple))
        )
        first_text = _first_generation_text(generations)
        if first_text is not None:
            attrs["completion_preview"] = _truncate(first_text)
    llm_output = getattr(response, "llm_output", None)
    if isinstance(llm_output, Mapping):
        usage = llm_output.get("token_usage") or llm_output.get("usage")
        if isinstance(usage, Mapping):
            for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                if key in usage:
                    attrs[key] = usage[key]
        model = llm_output.get("model_name") or llm_output.get("model")
        if model:
            attrs["model_name"] = str(model)
    return attrs


def _first_generation_text(generations: Sequence[Any]) -> Optional[str]:
    try:
        gen = generations[0][0]
    except (IndexError, TypeError):
        return None
    text = getattr(gen, "text", None)
    if isinstance(text, str) and text:
        return text
    message = getattr(gen, "message", None)
    content = getattr(message, "content", None)
    if isinstance(content, str) and content:
        return content
    return None


def _error_attributes(error: BaseException) -> Dict[str, Any]:
    return {"error_type": type(error).__name__, "message": _truncate(str(error))}


# ── The handler ─────────────────────────────────────────────────────────────────


class DProvenanceCallbackHandler(_BaseCallbackHandler):  # type: ignore[misc,valid-type]
    """A LangChain callback handler that records a DProvenanceKit run.

    Bind it to an :class:`~dprovenancekit.kit.ActiveTraceRun` (usually via
    :class:`DProvenanceTracer`) and pass it as a callback to any LangChain/LangGraph
    invocation. The handler holds its run directly, so it records correctly regardless of
    which thread or task LangChain dispatches a callback on.

    Options:
        capture_payloads: include prompt/completion/tool-IO previews in event attributes
            (truncated). Turn off to record only structural metadata (counts, keys).
        link_lifecycle: emit provenance edges (``DERIVED_FROM`` start→end, ``INFORMED``
            parent→child). Turn off to record events without edges.
        record_chains: record chain (Runnable) start/end events. LCEL/LangGraph emit many
            of these; turn off to keep traces focused on models, tools, and retrievers.
    """

    # Ask LangChain to dispatch us synchronously and in order, so per-run sequence
    # numbers reflect true execution order.
    run_inline = True

    def __init__(
        self,
        active_run: ActiveTraceRun,
        *,
        capture_payloads: bool = True,
        link_lifecycle: bool = True,
        record_chains: bool = True,
    ) -> None:
        self._run = active_run
        self._capture = capture_payloads
        self._link = link_lifecycle
        self._record_chains = record_chains
        # LangChain run_id (str) -> the trace event id recorded for that node's *start*.
        self._start_event: Dict[str, uuid.UUID] = {}

    # MARK: - Introspection ------------------------------------------------------

    @property
    def run_id(self) -> uuid.UUID:
        """The DProvenanceKit run id this handler is recording into."""
        return self._run.run_id

    @property
    def active_run(self) -> ActiveTraceRun:
        return self._run

    # MARK: - Core recording -----------------------------------------------------

    def _record(
        self,
        type_name: str,
        priority: TracePriority,
        attributes: Mapping[str, Any],
        *,
        engine: Optional[str],
        run_id: Any,
        parent_run_id: Any,
    ) -> uuid.UUID:
        payload = LangChainTraceEvent.make(type_name, priority, attributes)
        span = str(run_id) if run_id is not None else None
        parent = str(parent_run_id) if parent_run_id is not None else None
        # Set the span contextvars transiently: each callback runs to completion in one
        # thread/task, so set+reset within the call is correct and isolated.
        span_token = TraceContext.current_span_id.set(span)
        parent_token = TraceContext.parent_span_id.set(parent)
        try:
            event_id = self._run.record(payload, engine)
        finally:
            TraceContext.parent_span_id.reset(parent_token)
            TraceContext.current_span_id.reset(span_token)
        return event_id

    def _on_start(
        self,
        type_name: str,
        priority: TracePriority,
        attributes: Mapping[str, Any],
        *,
        engine: Optional[str],
        run_id: Any,
        parent_run_id: Any,
    ) -> None:
        event_id = self._record(
            type_name,
            priority,
            attributes,
            engine=engine,
            run_id=run_id,
            parent_run_id=parent_run_id,
        )
        key = str(run_id) if run_id is not None else None
        if key is not None:
            self._start_event[key] = event_id
        if self._link and parent_run_id is not None:
            parent_start = self._start_event.get(str(parent_run_id))
            if parent_start is not None:
                # The parent step's context informed this child step.
                self._run.link(parent_start, event_id, TraceEdgeType.INFORMED)

    def _on_finish(
        self,
        type_name: str,
        priority: TracePriority,
        attributes: Mapping[str, Any],
        *,
        engine: Optional[str],
        run_id: Any,
        parent_run_id: Any,
    ) -> None:
        event_id = self._record(
            type_name,
            priority,
            attributes,
            engine=engine,
            run_id=run_id,
            parent_run_id=parent_run_id,
        )
        if self._link and run_id is not None:
            start_id = self._start_event.pop(str(run_id), None)
            if start_id is not None:
                # The result was derived from the invocation that started this node.
                self._run.link(start_id, event_id, TraceEdgeType.DERIVED_FROM)

    # MARK: - LLM ----------------------------------------------------------------

    def on_llm_start(
        self,
        serialized: Any,
        prompts: List[str],
        *,
        run_id: Any,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> None:
        attrs: Dict[str, Any] = {"prompt_count": len(prompts) if prompts else 0}
        if self._capture and prompts:
            attrs["prompts"] = [_truncate(p) for p in prompts]
        self._on_start(
            LCEventType.LLM_STARTED,
            TracePriority.STRUCTURAL,
            attrs,
            engine=_component_name(serialized, kwargs, "LLM"),
            run_id=run_id,
            parent_run_id=parent_run_id,
        )

    def on_chat_model_start(
        self,
        serialized: Any,
        messages: Any,
        *,
        run_id: Any,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> None:
        count = 0
        if isinstance(messages, (list, tuple)):
            count = sum(len(g) for g in messages if isinstance(g, (list, tuple)))
        self._on_start(
            LCEventType.CHAT_MODEL_STARTED,
            TracePriority.STRUCTURAL,
            {"message_count": count},
            engine=_component_name(serialized, kwargs, "ChatModel"),
            run_id=run_id,
            parent_run_id=parent_run_id,
        )

    def on_llm_end(
        self, response: Any, *, run_id: Any, parent_run_id: Any = None, **kwargs: Any
    ) -> None:
        attrs = _llm_end_attributes(response)
        if not self._capture:
            attrs.pop("completion_preview", None)
        self._on_finish(
            LCEventType.LLM_ENDED,
            TracePriority.STRUCTURAL,
            attrs,
            engine=attrs.get("model_name") or "LLM",
            run_id=run_id,
            parent_run_id=parent_run_id,
        )

    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: Any,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> None:
        self._on_finish(
            LCEventType.LLM_ERROR,
            TracePriority.CRITICAL,
            _error_attributes(error),
            engine="LLM",
            run_id=run_id,
            parent_run_id=parent_run_id,
        )

    # MARK: - Chain --------------------------------------------------------------

    def on_chain_start(
        self,
        serialized: Any,
        inputs: Any,
        *,
        run_id: Any,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> None:
        if not self._record_chains:
            return
        attrs: Dict[str, Any] = {}
        input_keys = _keys(inputs)
        if input_keys is not None:
            attrs["input_keys"] = input_keys
        self._on_start(
            LCEventType.CHAIN_STARTED,
            TracePriority.STRUCTURAL,
            attrs,
            engine=_component_name(serialized, kwargs, "Chain"),
            run_id=run_id,
            parent_run_id=parent_run_id,
        )

    def on_chain_end(
        self, outputs: Any, *, run_id: Any, parent_run_id: Any = None, **kwargs: Any
    ) -> None:
        if not self._record_chains:
            return
        attrs: Dict[str, Any] = {}
        output_keys = _keys(outputs)
        if output_keys is not None:
            attrs["output_keys"] = output_keys
        self._on_finish(
            LCEventType.CHAIN_ENDED,
            TracePriority.STRUCTURAL,
            attrs,
            engine="Chain",
            run_id=run_id,
            parent_run_id=parent_run_id,
        )

    def on_chain_error(
        self,
        error: BaseException,
        *,
        run_id: Any,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> None:
        if not self._record_chains:
            return
        self._on_finish(
            LCEventType.CHAIN_ERROR,
            TracePriority.CRITICAL,
            _error_attributes(error),
            engine="Chain",
            run_id=run_id,
            parent_run_id=parent_run_id,
        )

    # MARK: - Tool ---------------------------------------------------------------

    def on_tool_start(
        self,
        serialized: Any,
        input_str: Any,
        *,
        run_id: Any,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> None:
        name = _component_name(serialized, kwargs, "Tool")
        attrs: Dict[str, Any] = {"tool": name}
        if self._capture and input_str is not None:
            attrs["input"] = _truncate(str(input_str))
        self._on_start(
            LCEventType.TOOL_STARTED,
            TracePriority.STRUCTURAL,
            attrs,
            engine=name,
            run_id=run_id,
            parent_run_id=parent_run_id,
        )

    def on_tool_end(
        self, output: Any, *, run_id: Any, parent_run_id: Any = None, **kwargs: Any
    ) -> None:
        attrs: Dict[str, Any] = {}
        if self._capture and output is not None:
            attrs["output"] = _truncate(str(output))
        self._on_finish(
            LCEventType.TOOL_ENDED,
            TracePriority.STRUCTURAL,
            attrs,
            engine="Tool",
            run_id=run_id,
            parent_run_id=parent_run_id,
        )

    def on_tool_error(
        self,
        error: BaseException,
        *,
        run_id: Any,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> None:
        self._on_finish(
            LCEventType.TOOL_ERROR,
            TracePriority.CRITICAL,
            _error_attributes(error),
            engine="Tool",
            run_id=run_id,
            parent_run_id=parent_run_id,
        )

    # MARK: - Retriever ----------------------------------------------------------

    def on_retriever_start(
        self,
        serialized: Any,
        query: Any,
        *,
        run_id: Any,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> None:
        attrs: Dict[str, Any] = {}
        if self._capture and query is not None:
            attrs["query"] = _truncate(str(query))
        self._on_start(
            LCEventType.RETRIEVER_STARTED,
            TracePriority.STRUCTURAL,
            attrs,
            engine=_component_name(serialized, kwargs, "Retriever"),
            run_id=run_id,
            parent_run_id=parent_run_id,
        )

    def on_retriever_end(
        self, documents: Any, *, run_id: Any, parent_run_id: Any = None, **kwargs: Any
    ) -> None:
        count = len(documents) if isinstance(documents, (list, tuple)) else None
        self._on_finish(
            LCEventType.RETRIEVER_ENDED,
            TracePriority.STRUCTURAL,
            {"document_count": count},
            engine="Retriever",
            run_id=run_id,
            parent_run_id=parent_run_id,
        )

    def on_retriever_error(
        self,
        error: BaseException,
        *,
        run_id: Any,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> None:
        self._on_finish(
            LCEventType.RETRIEVER_ERROR,
            TracePriority.CRITICAL,
            _error_attributes(error),
            engine="Retriever",
            run_id=run_id,
            parent_run_id=parent_run_id,
        )

    # MARK: - Agent --------------------------------------------------------------

    def on_agent_action(
        self, action: Any, *, run_id: Any, parent_run_id: Any = None, **kwargs: Any
    ) -> None:
        attrs: Dict[str, Any] = {}
        tool = getattr(action, "tool", None)
        if tool is not None:
            attrs["tool"] = str(tool)
        if self._capture:
            tool_input = getattr(action, "tool_input", None)
            if tool_input is not None:
                attrs["tool_input"] = _truncate(str(tool_input))
        # An agent action is a step inside an executor run; record it without disturbing
        # the start/end map (it is neither a span start nor end of its own node).
        self._record(
            LCEventType.AGENT_ACTION,
            TracePriority.STRUCTURAL,
            attrs,
            engine="Agent",
            run_id=run_id,
            parent_run_id=parent_run_id,
        )

    def on_agent_finish(
        self, finish: Any, *, run_id: Any, parent_run_id: Any = None, **kwargs: Any
    ) -> None:
        attrs: Dict[str, Any] = {}
        if self._capture:
            return_values = getattr(finish, "return_values", None)
            if isinstance(return_values, Mapping):
                output = return_values.get("output")
                if output is not None:
                    attrs["output"] = _truncate(str(output))
        # The final decision boundary: CRITICAL so it survives congestion.
        self._record(
            LCEventType.AGENT_FINISH,
            TracePriority.CRITICAL,
            attrs,
            engine="Agent",
            run_id=run_id,
            parent_run_id=parent_run_id,
        )


# ── Tracer (run lifecycle) ──────────────────────────────────────────────────────


class DProvenanceTracer:
    """Opens a DProvenanceKit run per invocation and hands you a handler for it.

        tracer = DProvenanceTracer(store)
        with tracer.trace(context_id="case-1") as cb:
            chain.invoke(x, config={"callbacks": [cb]})

    The run is flushed when the ``with`` block exits (normally or on error). After it
    closes, query / diff / fingerprint the run through the same ``store``.
    """

    def __init__(self, store: Any, *, schema_version: int = 1) -> None:
        self._store = store
        self._schema_version = schema_version

    @contextmanager
    def trace(
        self,
        context_id: str,
        *,
        capture_payloads: bool = True,
        link_lifecycle: bool = True,
        record_chains: bool = True,
    ) -> Iterator[DProvenanceCallbackHandler]:
        active = ActiveTraceRun(
            context_id=context_id,
            store=self._store,
            event_type=LangChainTraceEvent,
            schema_version=self._schema_version,
        )
        handler = DProvenanceCallbackHandler(
            active,
            capture_payloads=capture_payloads,
            link_lifecycle=link_lifecycle,
            record_chains=record_chains,
        )
        try:
            yield handler
        finally:
            active.flush()


__all__ = [
    "DProvenanceCallbackHandler",
    "DProvenanceTracer",
    "LangChainTraceEvent",
    "LCEventType",
]

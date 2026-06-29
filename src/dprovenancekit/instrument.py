"""Framework-agnostic instrumentation — trace a hand-written agent loop.

Not every agent uses LangChain or the OpenAI Agents SDK; plenty are a plain Python loop
that calls an LLM client and a few tools directly. This module records such code with no
framework dependency and no event type to define: open a run with :func:`traced_run`,
decorate the functions you care about with :func:`traced`, and drop :func:`record_event`
wherever a decision is made.

    from dprovenancekit import InMemoryTraceStore
    from dprovenancekit.instrument import traced, traced_run, record_event

    @traced                      # or @traced(name="search")
    def search(query): ...

    @traced
    def answer(question, sources): ...

    store = InMemoryTraceStore()
    with traced_run(store, context_id="ticket-42"):
        sources = search(question)             # -> search.start / search.end (+ span)
        record_event("plan.chosen", {"strategy": "rag"})
        reply = answer(question, sources)      # nested calls nest in the span tree

Each decorated call becomes its own **span** carrying a ``"<name>.start"`` / ``".end"`` /
``".error"`` event pair; the function name (or ``name=``) is the **engine**; nested calls
nest in the span tree; and lifecycle **provenance edges** are emitted (``DERIVED_FROM``
start→end, ``INFORMED`` enclosing-step→nested-step). Outside a ``traced_run`` the
decorators are transparent — they just call the wrapped function — so instrumented code is
safe to call untraced. Both sync and ``async def`` functions are supported.

Because everything flows through the normal recording path, the whole toolkit applies:
query the run, diff two runs, compare run fingerprints, or gate regressions.
"""

from __future__ import annotations

import functools
import inspect
import json
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterator, Mapping, Optional

from .context import TraceContext
from .edge import TraceEdgeType
from .event import TraceableEvent
from .kit import ActiveTraceRun, DProvenanceKit
from .priority import TracePriority

# The enclosing decorated step's *start* event id, so a nested step can be INFORMED by it.
_enclosing_step: ContextVar[Optional[uuid.UUID]] = ContextVar("dprov_enclosing_step", default=None)


def _jsonable(obj: Any) -> Any:
    return str(obj)


@dataclass(frozen=True)
class TracedEvent(TraceableEvent):
    """The generic event recorded by :mod:`dprovenancekit.instrument`.

    Attributes are stored as a canonical (sorted-key) JSON string so the event is hashable
    and two events with equal attributes compare equal (so exact-equality alignment works).
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
    ) -> "TracedEvent":
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
    def from_dict(cls, data: dict) -> "TracedEvent":
        attrs = {k: v for k, v in data.items() if k not in ("type", "priority")}
        return cls.make(
            type_name=data["type"],
            priority=TracePriority(int(data.get("priority", int(TracePriority.STRUCTURAL)))),
            attributes=attrs,
        )


# One module-level kit parameterized by TracedEvent. Recording reads the ambient run from
# contextvars, so a single kit instance serves every traced_run.
_KIT = DProvenanceKit(TracedEvent)


def _truncate(text: str, limit: int = 500) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def _summarize_call(args: tuple, kwargs: dict) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if args:
        out["args"] = [_truncate(repr(a)) for a in args]
    if kwargs:
        out["kwargs"] = {k: _truncate(repr(v)) for k, v in kwargs.items()}
    return out


@contextmanager
def traced_run(store: Any, context_id: str, *, schema_version: int = 1) -> Iterator[ActiveTraceRun]:
    """Open a recording run for instrumented code. Yields the active run; flushes on exit."""
    with _KIT.run(context_id=context_id, store=store, schema_version=schema_version) as run:
        try:
            yield run
        finally:
            run.flush()


def record_event(
    type_identifier: str,
    attributes: Optional[Mapping[str, Any]] = None,
    *,
    priority: TracePriority = TracePriority.STRUCTURAL,
) -> Optional[uuid.UUID]:
    """Record an ad-hoc event in the current run (a decision, a chosen branch, …).

    A soft no-op (returns ``None``) outside a :func:`traced_run`. Records under the current
    span/engine, so a call inside a decorated step attributes to that step.
    """
    return _KIT.record(TracedEvent.make(type_identifier, priority, attributes))


def _record(type_name: str, priority: TracePriority, attributes: Mapping[str, Any]) -> Optional[uuid.UUID]:
    return _KIT.record(TracedEvent.make(type_name, priority, attributes))


def _link(source: Optional[uuid.UUID], target: Optional[uuid.UUID], edge: TraceEdgeType) -> None:
    if source is not None and target is not None:
        _KIT.link(source, target, edge)


def traced(
    fn: Optional[Callable] = None,
    *,
    name: Optional[str] = None,
    capture_args: bool = True,
    capture_result: bool = True,
    link_lifecycle: bool = True,
    priority: TracePriority = TracePriority.STRUCTURAL,
) -> Callable:
    """Decorator: record a function call as a traced step (``@traced`` or ``@traced(...)``).

    On each call within a :func:`traced_run`, opens a fresh span and records
    ``"<name>.start"`` then ``"<name>.end"`` (or ``"<name>.error"`` at ``CRITICAL`` if it
    raises). Outside a run the wrapper is transparent. ``async def`` functions are awaited.
    """

    def decorate(func: Callable) -> Callable:
        step_name = name or getattr(func, "__name__", "step")

        def _fail(error: BaseException):
            _record(
                f"{step_name}.error",
                TracePriority.CRITICAL,
                {"name": step_name, "error_type": type(error).__name__, "message": _truncate(str(error))},
            )

        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def awrapper(*args, **kwargs):
                if TraceContext.current_run.get() is None:
                    return await func(*args, **kwargs)
                with _KIT.with_span(), _KIT.with_engine(step_name):
                    attrs: Dict[str, Any] = {"name": step_name}
                    if capture_args:
                        attrs.update(_summarize_call(args, kwargs))
                    start_id = _record(f"{step_name}.start", priority, attrs)
                    if link_lifecycle:
                        _link(_enclosing_step.get(), start_id, TraceEdgeType.INFORMED)
                    token = _enclosing_step.set(start_id)
                    try:
                        result = await func(*args, **kwargs)
                    except BaseException as error:  # noqa: BLE001 - record then re-raise
                        _fail(error)
                        raise
                    finally:
                        _enclosing_step.reset(token)
                    end_attrs: Dict[str, Any] = {"name": step_name}
                    if capture_result:
                        end_attrs["result"] = _truncate(repr(result))
                    end_id = _record(f"{step_name}.end", priority, end_attrs)
                    if link_lifecycle:
                        _link(start_id, end_id, TraceEdgeType.DERIVED_FROM)
                    return result

            return awrapper

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            if TraceContext.current_run.get() is None:
                return func(*args, **kwargs)
            with _KIT.with_span(), _KIT.with_engine(step_name):
                attrs: Dict[str, Any] = {"name": step_name}
                if capture_args:
                    attrs.update(_summarize_call(args, kwargs))
                start_id = _record(f"{step_name}.start", priority, attrs)
                if link_lifecycle:
                    _link(_enclosing_step.get(), start_id, TraceEdgeType.INFORMED)
                token = _enclosing_step.set(start_id)
                try:
                    result = func(*args, **kwargs)
                except BaseException as error:  # noqa: BLE001 - record then re-raise
                    _fail(error)
                    raise
                finally:
                    _enclosing_step.reset(token)
                end_attrs: Dict[str, Any] = {"name": step_name}
                if capture_result:
                    end_attrs["result"] = _truncate(repr(result))
                end_id = _record(f"{step_name}.end", priority, end_attrs)
                if link_lifecycle:
                    _link(start_id, end_id, TraceEdgeType.DERIVED_FROM)
                return result

        return wrapper

    return decorate(fn) if fn is not None else decorate


__all__ = [
    "TracedEvent",
    "traced",
    "traced_run",
    "record_event",
]

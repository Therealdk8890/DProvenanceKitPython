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
start→end *and* start→error, ``INFORMED`` enclosing-step→nested-step). Outside a
``traced_run`` the decorators are transparent — they just call the wrapped function — so
instrumented code is safe to call untraced.

Supported call shapes: plain functions, ``async def`` coroutines, generators, and async
generators. For a generator the start/end bracket the *whole iteration* (and an error
raised mid-iteration is recorded), but — to avoid leaking span context across ``yield``
points — steps invoked *while a generator is being iterated* are not nested under it.

Instrumentation never changes the behavior of the wrapped call: argument/result/error
capture is failure-proof (a value whose ``repr()`` raises is recorded as a placeholder,
never propagated), exceptions are recorded and re-raised unchanged, and return values pass
through untouched.

**Threads:** recording relies on ``contextvars`` propagation. Coroutines, ``asyncio``
tasks/``gather``, and ``asyncio.to_thread`` inherit the run context and record normally; a
sync function hopped to a *bare* executor (``loop.run_in_executor(None, fn)`` /
``Executor.submit(fn)``) runs without the context and is silently not recorded. Prefer
``asyncio.to_thread`` (it copies the context), or copy it yourself with
``contextvars.copy_context()``.
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


def _safe_repr(obj: Any) -> str:
    """``repr`` that can never raise — instrumentation must not crash the wrapped call."""
    try:
        return _truncate(repr(obj))
    except BaseException as exc:  # noqa: BLE001
        return f"<unreprable {type(obj).__name__}: {type(exc).__name__}>"


def _safe_str(obj: Any) -> str:
    try:
        return _truncate(str(obj))
    except BaseException as exc:  # noqa: BLE001
        return f"<unstrable {type(obj).__name__}: {type(exc).__name__}>"


def _summarize_call(args: tuple, kwargs: dict) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if args:
        out["args"] = [_safe_repr(a) for a in args]
    if kwargs:
        out["kwargs"] = {k: _safe_repr(v) for k, v in kwargs.items()}
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


def _record_in_span(
    type_name: str,
    priority: TracePriority,
    attributes: Mapping[str, Any],
    *,
    engine: str,
    span_id: str,
    parent_span_id: Optional[str],
) -> Optional[uuid.UUID]:
    """Record a single event under an explicit span/engine, set transiently and reset
    immediately. Used for generator start/end/error so no context is held across a yield."""
    span_token = TraceContext.current_span_id.set(span_id)
    parent_token = TraceContext.parent_span_id.set(parent_span_id)
    engine_token = TraceContext.engine_stack.set(list(TraceContext.engine_stack.get()) + [engine])
    try:
        return _record(type_name, priority, attributes)
    finally:
        TraceContext.engine_stack.reset(engine_token)
        TraceContext.parent_span_id.reset(parent_token)
        TraceContext.current_span_id.reset(span_token)


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
    raises). Outside a run the wrapper is transparent. Plain functions, ``async def``,
    generators, and async generators are all supported; for generators the start/end
    bracket the full iteration.
    """

    def decorate(func: Callable) -> Callable:
        step_name = name or getattr(func, "__name__", "step")

        def _start_attrs(args, kwargs) -> Dict[str, Any]:
            attrs: Dict[str, Any] = {"name": step_name}
            if capture_args:
                attrs.update(_summarize_call(args, kwargs))
            return attrs

        def _end_attrs(result) -> Dict[str, Any]:
            attrs: Dict[str, Any] = {"name": step_name}
            if capture_result:
                attrs["result"] = _safe_repr(result)
            return attrs

        def _err_attrs(error) -> Dict[str, Any]:
            return {"name": step_name, "error_type": type(error).__name__, "message": _safe_str(error)}

        # ── async generator: bracket the whole async iteration ──────────────────
        if inspect.isasyncgenfunction(func):

            @functools.wraps(func)
            async def agwrapper(*args, **kwargs):
                if TraceContext.current_run.get() is None:
                    async for item in func(*args, **kwargs):
                        yield item
                    return
                span_id = str(uuid.uuid4())
                parent = TraceContext.current_span_id.get()
                start_id = _record_in_span(
                    f"{step_name}.start", priority, _start_attrs(args, kwargs),
                    engine=step_name, span_id=span_id, parent_span_id=parent,
                )
                if link_lifecycle:
                    _link(_enclosing_step.get(), start_id, TraceEdgeType.INFORMED)
                try:
                    async for item in func(*args, **kwargs):
                        yield item
                except BaseException as error:  # noqa: BLE001 - record then re-raise
                    err_id = _record_in_span(
                        f"{step_name}.error", TracePriority.CRITICAL, _err_attrs(error),
                        engine=step_name, span_id=span_id, parent_span_id=parent,
                    )
                    if link_lifecycle:
                        _link(start_id, err_id, TraceEdgeType.DERIVED_FROM)
                    raise
                end_id = _record_in_span(
                    f"{step_name}.end", priority, {"name": step_name},
                    engine=step_name, span_id=span_id, parent_span_id=parent,
                )
                if link_lifecycle:
                    _link(start_id, end_id, TraceEdgeType.DERIVED_FROM)

            return agwrapper

        # ── sync generator: bracket the whole iteration (preserves yield-from) ──
        if inspect.isgeneratorfunction(func):

            @functools.wraps(func)
            def gwrapper(*args, **kwargs):
                if TraceContext.current_run.get() is None:
                    yield from func(*args, **kwargs)
                    return
                span_id = str(uuid.uuid4())
                parent = TraceContext.current_span_id.get()
                start_id = _record_in_span(
                    f"{step_name}.start", priority, _start_attrs(args, kwargs),
                    engine=step_name, span_id=span_id, parent_span_id=parent,
                )
                if link_lifecycle:
                    _link(_enclosing_step.get(), start_id, TraceEdgeType.INFORMED)
                try:
                    result = yield from func(*args, **kwargs)
                except BaseException as error:  # noqa: BLE001 - record then re-raise
                    err_id = _record_in_span(
                        f"{step_name}.error", TracePriority.CRITICAL, _err_attrs(error),
                        engine=step_name, span_id=span_id, parent_span_id=parent,
                    )
                    if link_lifecycle:
                        _link(start_id, err_id, TraceEdgeType.DERIVED_FROM)
                    raise
                end_id = _record_in_span(
                    f"{step_name}.end", priority, _end_attrs(result),
                    engine=step_name, span_id=span_id, parent_span_id=parent,
                )
                if link_lifecycle:
                    _link(start_id, end_id, TraceEdgeType.DERIVED_FROM)
                return result

            return gwrapper

        # ── async coroutine ─────────────────────────────────────────────────────
        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def awrapper(*args, **kwargs):
                if TraceContext.current_run.get() is None:
                    return await func(*args, **kwargs)
                with _KIT.with_span(), _KIT.with_engine(step_name):
                    start_id = _record(f"{step_name}.start", priority, _start_attrs(args, kwargs))
                    if link_lifecycle:
                        _link(_enclosing_step.get(), start_id, TraceEdgeType.INFORMED)
                    token = _enclosing_step.set(start_id)
                    try:
                        result = await func(*args, **kwargs)
                    except BaseException as error:  # noqa: BLE001 - record then re-raise
                        err_id = _record(f"{step_name}.error", TracePriority.CRITICAL, _err_attrs(error))
                        if link_lifecycle:
                            _link(start_id, err_id, TraceEdgeType.DERIVED_FROM)
                        raise
                    finally:
                        _enclosing_step.reset(token)
                    end_id = _record(f"{step_name}.end", priority, _end_attrs(result))
                    if link_lifecycle:
                        _link(start_id, end_id, TraceEdgeType.DERIVED_FROM)
                    return result

            return awrapper

        # ── plain function ──────────────────────────────────────────────────────
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            if TraceContext.current_run.get() is None:
                return func(*args, **kwargs)
            with _KIT.with_span(), _KIT.with_engine(step_name):
                start_id = _record(f"{step_name}.start", priority, _start_attrs(args, kwargs))
                if link_lifecycle:
                    _link(_enclosing_step.get(), start_id, TraceEdgeType.INFORMED)
                token = _enclosing_step.set(start_id)
                try:
                    result = func(*args, **kwargs)
                except BaseException as error:  # noqa: BLE001 - record then re-raise
                    err_id = _record(f"{step_name}.error", TracePriority.CRITICAL, _err_attrs(error))
                    if link_lifecycle:
                        _link(start_id, err_id, TraceEdgeType.DERIVED_FROM)
                    raise
                finally:
                    _enclosing_step.reset(token)
                end_id = _record(f"{step_name}.end", priority, _end_attrs(result))
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


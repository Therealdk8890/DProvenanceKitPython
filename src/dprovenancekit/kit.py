"""The public recording surface: ``DProvenanceKit``.

Mirrors the Swift ``DProvenanceKit<T>`` static API, but uses context managers in place
of trailing-closure scopes. Usage::

    kit = DProvenanceKit(MyEvent)
    with kit.run(context_id="case-123", store=store):
        kit.record(MyEvent.prompt_generated(150))
        with kit.with_engine("DocumentAnalyzer"):
            kit.record(MyEvent.document_evaluated("DocA", 0.95))
        kit.record(MyEvent.final_decision(True))
"""

from __future__ import annotations

import threading
import uuid
from contextlib import contextmanager
from typing import Iterator, Optional, Type

from .context import AnyActiveTraceRun, TraceContext
from .edge import TraceEdgeType
from .event import TraceableEvent, TraceEvent


class ActiveTraceRun(AnyActiveTraceRun):
    """A single in-flight trace run, owning the monotonic per-run sequence counter."""

    def __init__(self, context_id: str, store, event_type: Type, schema_version: int = 1):
        self.run_id = uuid.uuid4()
        self.context_id = context_id
        self._store = store
        self._event_type = event_type
        self._schema_version = schema_version
        self._sequence_lock = threading.Lock()
        self._sequence_counter = 0

    def record(self, payload: TraceableEvent, engine_name: Optional[str]) -> uuid.UUID:
        with self._sequence_lock:
            seq = self._sequence_counter
            self._sequence_counter += 1

        event = TraceEvent(
            run_id=self.run_id,
            context_id=self.context_id,
            engine_name=engine_name if engine_name is not None else "Unknown",
            schema_version=self._schema_version,
            sequence=seq,
            span_id=TraceContext.current_span_id.get(),
            parent_span_id=TraceContext.parent_span_id.get(),
            payload=payload,
        )
        self._store.record(event)
        return event.id

    def record_any(self, payload, engine_name: Optional[str]) -> Optional[uuid.UUID]:
        if not isinstance(payload, self._event_type):
            return None
        return self.record(payload, engine_name)

    def link(self, source: uuid.UUID, target: uuid.UUID, type: TraceEdgeType) -> None:
        # Reject self-referential edges at the write boundary — never valid provenance.
        if source == target:
            return
        self._store.link(source, target, type)

    def flush(self) -> None:
        self._store.flush()


class DProvenanceKit:
    """Recording entry point, parameterized by the consumer's event type."""

    def __init__(self, event_type: Type[TraceableEvent]):
        self.event_type = event_type

    @contextmanager
    def run(self, context_id: str, store, schema_version: int = 1) -> Iterator[ActiveTraceRun]:
        active = ActiveTraceRun(context_id, store, self.event_type, schema_version)
        token = TraceContext.current_run.set(active)
        try:
            yield active
        finally:
            TraceContext.current_run.reset(token)

    @contextmanager
    def with_engine(self, name: str) -> Iterator[None]:
        new_stack = list(TraceContext.engine_stack.get()) + [name]
        token = TraceContext.engine_stack.set(new_stack)
        try:
            yield
        finally:
            TraceContext.engine_stack.reset(token)

    @contextmanager
    def with_span(self, named: Optional[str] = None) -> Iterator[None]:
        new_span_id = named if named is not None else str(uuid.uuid4())
        parent = TraceContext.current_span_id.get()
        span_token = TraceContext.current_span_id.set(new_span_id)
        parent_token = TraceContext.parent_span_id.set(parent)
        try:
            yield
        finally:
            TraceContext.parent_span_id.reset(parent_token)
            TraceContext.current_span_id.reset(span_token)

    def record(self, payload: TraceableEvent) -> Optional[uuid.UUID]:
        run = TraceContext.current_run.get()
        if run is None:
            # Soft failure for executions outside of DProvenanceKit.run.
            return None
        engine_stack = TraceContext.engine_stack.get()
        last_engine = engine_stack[-1] if engine_stack else None
        return run.record_any(payload, last_engine)

    def link(self, source: uuid.UUID, target: uuid.UUID, type: TraceEdgeType) -> None:
        run = TraceContext.current_run.get()
        if run is None:
            return
        run.link(source, target, type)

    def flush(self) -> None:
        run = TraceContext.current_run.get()
        if run is None:
            return
        run.flush()

# git-blob-rewrite

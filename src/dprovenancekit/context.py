"""Ambient run / engine / span context, propagated via :mod:`contextvars`.

Recording uses no logger handle and no explicit context threading. The current run,
engine stack, and span lineage live in context variables — the Python analogue of
Swift's ``@TaskLocal`` — which propagate correctly across function and async boundaries.
Recording outside a run scope is a soft no-op rather than a crash.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar
from typing import List, Optional


class AnyActiveTraceRun:
    """Type-erased active run handle. Concrete runs implement these methods."""

    def record_any(self, payload, engine_name: Optional[str]) -> Optional[uuid.UUID]:
        raise NotImplementedError

    def link(self, source: uuid.UUID, target: uuid.UUID, type) -> None:
        raise NotImplementedError

    def flush(self) -> None:
        raise NotImplementedError


class TraceContext:
    current_run: ContextVar[Optional[AnyActiveTraceRun]] = ContextVar(
        "dprov_current_run", default=None
    )
    engine_stack: ContextVar[List[str]] = ContextVar("dprov_engine_stack", default=[])
    current_span_id: ContextVar[Optional[str]] = ContextVar(
        "dprov_current_span_id", default=None
    )
    parent_span_id: ContextVar[Optional[str]] = ContextVar(
        "dprov_parent_span_id", default=None
    )


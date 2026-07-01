"""Incremental, subscription-based live query evaluation."""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from typing import Dict, Set

from .event import TraceEvent
from .query import TraceQueryDSL, TraceQueryPlanner, TraceRun


class TraceQuerySubscription:
    """A live subscription. Concrete subscriptions provide ``query_id``, ``query`` and
    ``on_match`` / ``on_update`` callbacks."""

    query_id: uuid.UUID
    query: TraceQueryDSL

    def on_match(self, run: TraceRun) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def on_update(self, run: TraceRun) -> None:  # pragma: no cover - overridden
        raise NotImplementedError


@dataclass
class QueryState:
    matching_runs: Set[uuid.UUID] = field(default_factory=set)


class LiveTraceQueryEngine:
    """Evaluates registered subscriptions incrementally as events arrive.

    Thread-safe (the Python analogue of the Swift actor): a single lock guards the
    subscription tables and per-query match state.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscriptions: Dict[uuid.UUID, TraceQuerySubscription] = {}
        self._query_states: Dict[uuid.UUID, QueryState] = {}
        self._impacted_by_type: Dict[str, Set[uuid.UUID]] = {}
        self._global_subscriptions: Set[uuid.UUID] = set()

    def register(self, subscription: TraceQuerySubscription) -> None:
        with self._lock:
            self._subscriptions[subscription.query_id] = subscription
            self._query_states[subscription.query_id] = QueryState()
            referenced = TraceQueryPlanner.extract_all_referenced_decision_types(
                subscription.query.ast
            )
            if not referenced:
                self._global_subscriptions.add(subscription.query_id)
            else:
                for type_ in referenced:
                    self._impacted_by_type.setdefault(type_, set()).add(subscription.query_id)

    def process(self, event: TraceEvent, run: TraceRun) -> None:
        with self._lock:
            event_type = event.payload.type_identifier
            candidates = set(self._impacted_by_type.get(event_type, set()))
            candidates |= self._global_subscriptions
            if not candidates:
                candidates = set(self._subscriptions.keys())

            for query_id in candidates:
                subscription = self._subscriptions.get(query_id)
                if subscription is None:
                    continue
                state = self._query_states.get(query_id) or QueryState()
                is_match = subscription.query.ast.evaluate(run)
                previously = run.run_id in state.matching_runs

                if is_match:
                    if not previously:
                        state.matching_runs.add(run.run_id)
                        subscription.on_match(run)
                    else:
                        subscription.on_update(run)
                else:
                    if previously:
                        state.matching_runs.discard(run.run_id)

                self._query_states[query_id] = state

# git-blob-rewrite

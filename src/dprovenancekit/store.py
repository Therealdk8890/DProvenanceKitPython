"""The ``TraceStore`` protocol, the in-memory store, and graph-traversal helpers."""

from __future__ import annotations

import queue
import threading
import uuid
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Set

from .drop_stats import TraceDropStats
from .edge import TraceEdge, TraceEdgeType
from .event import TraceEvent
from .graph import TraceExplanation, TraceGraph
from .query import TraceQueryDSL, TraceQueryPlanner, TraceRun


class TraceError(Exception):
    pass


class NodeNotFoundError(TraceError):
    def __init__(self, node_id: uuid.UUID):
        super().__init__(f"node not found: {node_id}")
        self.node_id = node_id


class NotImplementedTraceError(TraceError):
    pass


class TraceStore(ABC):
    """A store of trace events and provenance edges."""

    @abstractmethod
    def record(self, event: TraceEvent) -> None: ...

    @abstractmethod
    def link(self, source: uuid.UUID, target: uuid.UUID, type: TraceEdgeType) -> None: ...

    @abstractmethod
    def flush(self) -> None: ...

    @abstractmethod
    def query_runs(self, dsl: TraceQueryDSL) -> List[TraceRun]: ...

    def query_quarantined_events(self, dsl: TraceQueryDSL) -> List[TraceEvent]:
        """Stores that do not quarantine events return nothing."""
        return []

    @property
    def drop_stats(self) -> TraceDropStats:
        """Stores that cannot shed report no drops."""
        return TraceDropStats()

    # MARK: - Graph traversal ----------------------------------------------------

    @abstractmethod
    def lineage_edges(self, id: uuid.UUID) -> List[TraceEdge]: ...

    @abstractmethod
    def impact_edges(self, id: uuid.UUID) -> List[TraceEdge]: ...

    @abstractmethod
    def get_events(self, ids: Set[uuid.UUID]) -> Dict[uuid.UUID, TraceEvent]: ...

    def lineage(self, id: uuid.UUID) -> TraceGraph:
        edges = self.lineage_edges(id)
        ids_to_fetch: Set[uuid.UUID] = {id}
        for edge in edges:
            ids_to_fetch.add(edge.source_id)
            ids_to_fetch.add(edge.target_id)
        nodes = self.get_events(ids_to_fetch)
        return TraceGraph(nodes=nodes, edges=edges)

    def impact(self, id: uuid.UUID) -> TraceGraph:
        edges = self.impact_edges(id)
        ids_to_fetch: Set[uuid.UUID] = {id}
        for edge in edges:
            ids_to_fetch.add(edge.source_id)
            ids_to_fetch.add(edge.target_id)
        nodes = self.get_events(ids_to_fetch)
        return TraceGraph(nodes=nodes, edges=edges)

    def explain(self, id: uuid.UUID) -> TraceExplanation:
        graph = self.lineage(id)
        target = graph.nodes.get(id)
        if target is None:
            raise NodeNotFoundError(id)

        target_summary = repr(target.payload)
        informed_by: List[str] = []
        derived_from: List[str] = []

        for edge in graph.edges:
            if edge.target_id != id:
                continue
            source = graph.nodes.get(edge.source_id)
            if source is None:
                continue
            summary = repr(source.payload)
            if edge.type == TraceEdgeType.INFORMED:
                informed_by.append(summary)
            elif edge.type == TraceEdgeType.DERIVED_FROM:
                derived_from.append(summary)

        return TraceExplanation(
            target_node_id=id,
            target_node_summary=target_summary,
            informed_by=informed_by,
            derived_from=derived_from,
        )


class InMemoryTraceStore(TraceStore):
    """An in-memory store for fast, localized execution and querying.

    ``record`` commits synchronously and in order: once it returns the event is
    queryable, and ``flush`` is a no-op barrier. When a ``LiveTraceQueryEngine`` is
    supplied, events are delivered to it in FIFO order over a background consumer
    thread, so live match state stays consistent under concurrent ingestion.
    """

    def __init__(self, live_engine=None):
        self._lock = threading.Lock()
        self._events_by_run: Dict[uuid.UUID, List[TraceEvent]] = {}
        self._edges: List[TraceEdge] = []
        self._run_by_context: Dict[str, Set[uuid.UUID]] = {}
        self._run_by_engine: Dict[str, Set[uuid.UUID]] = {}
        self._decision_type_events: Dict[str, Dict[uuid.UUID, List[float]]] = {}

        self._live_engine = live_engine
        self._live_queue: Optional[queue.Queue] = None
        self._live_thread: Optional[threading.Thread] = None
        if live_engine is not None:
            self._live_queue = queue.Queue()
            self._live_thread = threading.Thread(
                target=self._drain_live, name="dprov-live-consumer", daemon=True
            )
            self._live_thread.start()

    def _drain_live(self) -> None:
        assert self._live_queue is not None
        while True:
            item = self._live_queue.get()
            if item is None:  # sentinel
                return
            event, run = item
            self._live_engine.process(event=event, run=run)

    def record(self, event: TraceEvent) -> None:
        with self._lock:
            self._events_by_run.setdefault(event.run_id, []).append(event)
            self._run_by_context.setdefault(event.context_id, set()).add(event.run_id)
            self._run_by_engine.setdefault(event.engine_name, set()).add(event.run_id)
            type_id = event.payload.type_identifier
            self._decision_type_events.setdefault(type_id, {}).setdefault(
                event.run_id, []
            ).append(event.timestamp)
            snapshot = None if self._live_queue is None else self._make_run_locked(event.run_id)

        if snapshot is not None:
            self._live_queue.put((event, snapshot))

    def link(self, source: uuid.UUID, target: uuid.UUID, type: TraceEdgeType) -> None:
        with self._lock:
            self._edges.append(TraceEdge(source_id=source, target_id=target, type=type))

    def flush(self) -> None:
        # No-op: `record` commits synchronously, so nothing is pending to drain.
        pass

    def get_run(self, id: uuid.UUID) -> Optional[TraceRun]:
        with self._lock:
            return self._make_run_locked(id)

    def lineage_edges(self, id: uuid.UUID) -> List[TraceEdge]:
        with self._lock:
            result: List[TraceEdge] = []
            queue_ = [id]
            visited: Set[uuid.UUID] = set()
            while queue_:
                current = queue_.pop(0)
                if current in visited:
                    continue
                visited.add(current)
                incoming = [e for e in self._edges if e.target_id == current]
                result.extend(incoming)
                queue_.extend(e.source_id for e in incoming)
            return result

    def impact_edges(self, id: uuid.UUID) -> List[TraceEdge]:
        with self._lock:
            result: List[TraceEdge] = []
            queue_ = [id]
            visited: Set[uuid.UUID] = set()
            while queue_:
                current = queue_.pop(0)
                if current in visited:
                    continue
                visited.add(current)
                outgoing = [e for e in self._edges if e.source_id == current]
                result.extend(outgoing)
                queue_.extend(e.target_id for e in outgoing)
            return result

    def get_events(self, ids: Set[uuid.UUID]) -> Dict[uuid.UUID, TraceEvent]:
        with self._lock:
            result: Dict[uuid.UUID, TraceEvent] = {}
            for run_events in self._events_by_run.values():
                for event in run_events:
                    if event.id in ids:
                        result[event.id] = event
            return result

    def _make_run_locked(self, id: uuid.UUID) -> Optional[TraceRun]:
        """Build a run snapshot ordered by the authoritative causal clock (sequence)."""
        events = self._events_by_run.get(id)
        if events is None:
            return None
        sorted_events = sorted(events, key=lambda e: e.sequence)
        if not sorted_events:
            return None
        return TraceRun(
            run_id=id,
            context_id=sorted_events[0].context_id,
            events=sorted_events,
        )

    def query_runs(self, dsl: TraceQueryDSL) -> List[TraceRun]:
        with self._lock:
            # Phase 1: Candidate narrowing through inverted indices.
            constraints = TraceQueryPlanner.extract_guaranteed_constraints(dsl.ast)
            candidate_run_ids: Optional[Set[uuid.UUID]] = None
            for constraint in constraints:
                if constraint.kind == "contextID":
                    matching = self._run_by_context.get(constraint.value, set())
                elif constraint.kind == "engineName":
                    matching = self._run_by_engine.get(constraint.value, set())
                else:  # decisionType
                    matching = set(self._decision_type_events.get(constraint.value, {}).keys())

                if candidate_run_ids is None:
                    candidate_run_ids = set(matching)
                else:
                    candidate_run_ids &= matching

            final_candidates = (
                candidate_run_ids
                if candidate_run_ids is not None
                else set(self._events_by_run.keys())
            )

            # Phase 2: Full AST evaluation per run.
            matching_runs: List[TraceRun] = []
            for run_id in final_candidates:
                run = self._make_run_locked(run_id)
                if run is None:
                    continue
                if dsl.ast.evaluate(run):
                    matching_runs.append(run)
            return matching_runs

"""The ``TraceStore`` protocol, the in-memory store, and graph-traversal helpers."""

from __future__ import annotations

import logging
import queue
import threading
import uuid
from dataclasses import asdict, replace
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Set

from .drop_stats import TraceDropStats
from .edge import TraceEdge, TraceEdgeType
from .event import TraceEvent
from .graph import TraceExplanation, TraceGraph
from .query import CaptureQuality, TraceQueryDSL, TraceQueryPlanner, TraceRun

logger = logging.getLogger(__name__)


class TraceError(Exception):
    pass


class NodeNotFoundError(TraceError):
    def __init__(self, node_id: uuid.UUID):
        super().__init__(f"node not found: {node_id}")
        self.node_id = node_id


class NotImplementedTraceError(TraceError):
    pass


def validate_max_depth(max_depth: Optional[int]) -> None:
    if max_depth is not None and max_depth < 0:
        raise ValueError(f"max_depth must be non-negative, got {max_depth}")


class TraceStore(ABC):
    """A store of trace events and provenance edges."""

    @abstractmethod
    def record(self, event: TraceEvent) -> None: ...

    @abstractmethod
    def link(
        self, source: uuid.UUID, target: uuid.UUID, type: TraceEdgeType
    ) -> None: ...

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
    def lineage_edges(
        self, id: uuid.UUID, max_depth: Optional[int] = None
    ) -> List[TraceEdge]: ...

    @abstractmethod
    def impact_edges(
        self, id: uuid.UUID, max_depth: Optional[int] = None
    ) -> List[TraceEdge]: ...

    @abstractmethod
    def get_events(self, ids: Set[uuid.UUID]) -> Dict[uuid.UUID, TraceEvent]: ...

    def lineage(self, id: uuid.UUID, max_depth: Optional[int] = None) -> TraceGraph:
        """The ancestry graph of ``id``: every edge on a path into it.

        ``max_depth`` bounds the walk to edges within that many hops of ``id``
        (``None`` walks the whole connected ancestry) — cap it when traces may
        contain long tool loops.
        """
        edges = self.lineage_edges(id, max_depth=max_depth)
        ids_to_fetch: Set[uuid.UUID] = {id}
        for edge in edges:
            ids_to_fetch.add(edge.source_id)
            ids_to_fetch.add(edge.target_id)
        nodes = self.get_events(ids_to_fetch)
        return TraceGraph(nodes=nodes, edges=edges)

    def impact(self, id: uuid.UUID, max_depth: Optional[int] = None) -> TraceGraph:
        """The descendants graph of ``id``: every edge on a path out of it.

        ``max_depth`` bounds the walk to edges within that many hops of ``id``
        (``None`` walks the whole connected impact set).
        """
        edges = self.impact_edges(id, max_depth=max_depth)
        ids_to_fetch: Set[uuid.UUID] = {id}
        for edge in edges:
            ids_to_fetch.add(edge.source_id)
            ids_to_fetch.add(edge.target_id)
        nodes = self.get_events(ids_to_fetch)
        return TraceGraph(nodes=nodes, edges=edges)

    def explain(self, id: uuid.UUID) -> TraceExplanation:
        # Only direct parents feed the explanation, so depth 1 suffices.
        graph = self.lineage(id, max_depth=1)
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
        self._capture_quality: Dict[uuid.UUID, CaptureQuality] = {}
        self._capture_contexts: Dict[uuid.UUID, str] = {}
        self._event_runs: Dict[uuid.UUID, uuid.UUID] = {}
        self._closed = False

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
            try:
                self._live_engine.process(event=event, run=run)
            except Exception:
                # A subscriber callback raising must not kill this shared daemon consumer
                # and silently stop *all* live delivery (while `record` keeps enqueuing
                # into an unbounded queue). Log and continue with the next event.
                logger.exception(
                    "[DProvenanceKit] live subscription handler raised; "
                    "continuing delivery"
                )

    def close(self) -> None:
        with self._lock:
            self._closed = True
            for key, quality in self._capture_quality.items():
                if not quality.sealed:
                    self._capture_quality[key] = replace(quality, lifecycle="interrupted", sealed=True)
        if self._live_queue is not None and self._live_thread is not None:
            self._live_queue.put(None)
            self._live_thread.join()
            self._live_thread = None

    def __enter__(self) -> "InMemoryTraceStore":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def record(self, event: TraceEvent) -> None:
        with self._lock:
            self._check_write_locked(event.run_id)
            quality = self._capture_quality.get(event.run_id)
            if quality is not None and quality.attempted_events is not None:
                self._capture_quality[event.run_id] = replace(
                    quality, attempted_events=quality.attempted_events + 1
                )
            self._event_runs[event.id] = event.run_id
            self._events_by_run.setdefault(event.run_id, []).append(event)
            self._run_by_context.setdefault(event.context_id, set()).add(event.run_id)
            self._run_by_engine.setdefault(event.engine_name, set()).add(event.run_id)
            type_id = event.payload.type_identifier
            self._decision_type_events.setdefault(type_id, {}).setdefault(
                event.run_id, []
            ).append(event.timestamp)
            snapshot = (
                None
                if self._live_queue is None
                else self._make_run_locked(event.run_id)
            )
            if snapshot is not None and self._live_queue is not None:
                self._live_queue.put((event, snapshot))

    def link(self, source: uuid.UUID, target: uuid.UUID, type: TraceEdgeType) -> None:
        with self._lock:
            run_id = self._event_runs.get(source) or self._event_runs.get(target)
            self._link_locked(run_id, source, target, type)

    def link_for_run(self, run_id, source, target, type) -> None:
        with self._lock:
            self._link_locked(run_id, source, target, type)

    def _link_locked(self, run_id, source, target, type) -> None:
        self._check_write_locked(run_id)
        quality = self._capture_quality.get(run_id)
        if quality is not None and quality.attempted_edges is not None:
            self._capture_quality[run_id] = replace(quality, attempted_edges=quality.attempted_edges + 1)
        self._edges.append(TraceEdge(source_id=source, target_id=target, type=type))

    def _check_write_locked(self, run_id) -> None:
        if self._closed:
            raise RuntimeError("trace store is closed")
        quality = self._capture_quality.get(run_id)
        if quality is not None and quality.sealed:
            raise RuntimeError("trace run is sealed; late writes are rejected")

    def begin_run(self, run_id, context_id, schema_version=1) -> None:
        with self._lock:
            self._check_write_locked(run_id)
            if run_id in self._capture_quality:
                raise ValueError("run already exists")
            self._capture_contexts[run_id] = context_id
            self._capture_quality[run_id] = CaptureQuality(
                lifecycle="recording", attempted_events=0, attempted_edges=0,
                schema_provenance="recorded",
            )

    def finish_run(self, run_id, status="completed") -> None:
        if status not in ("completed", "failed", "interrupted"):
            raise ValueError("invalid run completion status")
        with self._lock:
            self._check_write_locked(run_id)
            quality = self._capture_quality.get(run_id)
            if quality is None:
                raise ValueError("begin_run is required before finish_run")
            self._capture_quality[run_id] = replace(quality, lifecycle=status, sealed=True)

    def bind_workflow(self, run_id, workflow_id) -> None:
        with self._lock:
            self._check_write_locked(run_id)
            quality = self._capture_quality.get(run_id)
            if (quality is None or quality.lifecycle != "recording"
                    or quality.attempted_events != 0 or quality.attempted_edges != 0):
                raise ValueError("workflow must be bound before recording begins")
            if quality.workflow_id is not None and quality.workflow_id != workflow_id:
                raise ValueError("run already bound to another workflow")
            self._capture_quality[run_id] = replace(quality, workflow_id=workflow_id)

    def prepare_evidence(self, run_id) -> TraceRun:
        run_id = uuid.UUID(str(run_id))
        with self._lock:
            quality = self._capture_quality.get(run_id, CaptureQuality())
            if quality.lifecycle == "recording":
                raise ValueError("finish the run before preparing evidence")
            run = self._make_run_locked(run_id)
            if run is None:
                raise ValueError("run not found")
            return run

    def repository_run_snapshot(self, run_id) -> dict:
        run_id = uuid.UUID(str(run_id))
        with self._lock:
            run = self._make_run_locked(run_id)
            if run is None or run.capture_quality.lifecycle == "recording":
                raise ValueError("finish the run before preparing evidence")
            events = []
            for event in run.events:
                raw = event.payload.encode()
                events.append({
                    "id": str(event.id), "run_id": str(event.run_id),
                    "context_id": event.context_id, "engine_name": event.engine_name,
                    "schema_version": event.schema_version, "sequence": event.sequence,
                    "span_id": event.span_id, "parent_span_id": event.parent_span_id,
                    "type_identifier": event.payload.type_identifier,
                    "priority": int(event.payload.priority), "payload_bytes": raw,
                    "payload_json": raw.decode("utf-8"),
                    "timestamp_us": int(event.timestamp * 1_000_000),
                    "schema_provenance": "recorded",
                })
            ids = {event.id for event in run.events}
            edges = [{"source_id": str(edge.source_id), "target_id": str(edge.target_id),
                      "type": edge.type.value} for edge in self._edges
                     if edge.source_id in ids or edge.target_id in ids]
            return {"run": run, "events": events, "edges": edges,
                    "capture_quality": asdict(run.capture_quality),
                    "lifecycle": run.capture_quality.lifecycle}

    def flush(self) -> None:
        # No-op: `record` commits synchronously, so nothing is pending to drain.
        pass

    def get_run(self, id: uuid.UUID) -> Optional[TraceRun]:
        with self._lock:
            return self._make_run_locked(id)

    def lineage_edges(
        self, id: uuid.UUID, max_depth: Optional[int] = None
    ) -> List[TraceEdge]:
        validate_max_depth(max_depth)
        with self._lock:
            result: List[TraceEdge] = []
            # BFS, so each node is first reached at its minimal depth and the
            # depth cap never prunes an edge that a shorter path would keep.
            queue_ = [(id, 0)]
            visited: Set[uuid.UUID] = set()
            while queue_:
                current, depth = queue_.pop(0)
                if current in visited:
                    continue
                visited.add(current)
                if max_depth is not None and depth >= max_depth:
                    continue
                incoming = [e for e in self._edges if e.target_id == current]
                result.extend(incoming)
                queue_.extend((e.source_id, depth + 1) for e in incoming)
            return result

    def impact_edges(
        self, id: uuid.UUID, max_depth: Optional[int] = None
    ) -> List[TraceEdge]:
        validate_max_depth(max_depth)
        with self._lock:
            result: List[TraceEdge] = []
            queue_ = [(id, 0)]
            visited: Set[uuid.UUID] = set()
            while queue_:
                current, depth = queue_.pop(0)
                if current in visited:
                    continue
                visited.add(current)
                if max_depth is not None and depth >= max_depth:
                    continue
                outgoing = [e for e in self._edges if e.source_id == current]
                result.extend(outgoing)
                queue_.extend((e.target_id, depth + 1) for e in outgoing)
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
        events = self._events_by_run.get(id, [])
        if not events and id not in self._capture_contexts:
            return None
        sorted_events = sorted(events, key=lambda e: e.sequence)
        return TraceRun(
            run_id=id,
            context_id=sorted_events[0].context_id if sorted_events else self._capture_contexts[id],
            events=sorted_events,
            capture_quality=self._capture_quality.get(id, CaptureQuality()),
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
                    matching = set(
                        self._decision_type_events.get(constraint.value, {}).keys()
                    )

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

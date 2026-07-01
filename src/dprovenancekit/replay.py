"""Deterministic replay: reconstruct a span tree from committed + quarantined events."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional

from .event import TraceEvent


class ReplaySource(Enum):
    COMMITTED = "committed"
    QUARANTINED = "quarantined"


@dataclass(frozen=True)
class ReplayEvent:
    source: ReplaySource
    event: TraceEvent
    replay_order: int


@dataclass(frozen=True)
class SpanNode:
    span_id: Optional[str]
    start_sequence: int
    end_sequence: int
    events: List[ReplayEvent]
    children: List["SpanNode"]
    contains_quarantined_events: bool


@dataclass(frozen=True)
class SequenceGap:
    lower_bound: int
    upper_bound: int


@dataclass(frozen=True)
class ReplayManifest:
    total_events: int
    committed_events: int
    quarantined_events: int
    orphaned_events: int
    duplicate_event_ids: int
    reconstructed_spans: int
    contaminated_spans: int
    sequence_gaps: List[SequenceGap]


@dataclass(frozen=True)
class ReplaySnapshotMetadata:
    generated_at: float
    max_sequence_included: Optional[int]
    source_counts: Dict[ReplaySource, int]


@dataclass(frozen=True)
class ReplaySnapshot:
    roots: List[SpanNode]
    orphaned_events: List[ReplayEvent]
    manifest: ReplayManifest
    metadata: ReplaySnapshotMetadata


class _NodeBuilder:
    __slots__ = (
        "span_id",
        "parent_span_id",
        "start_sequence",
        "end_sequence",
        "events",
        "children",
        "contains_quarantined_events",
    )

    def __init__(self, span_id: Optional[str], parent_span_id: Optional[str]):
        self.span_id = span_id
        self.parent_span_id = parent_span_id
        self.start_sequence = 2**64
        self.end_sequence = 0
        self.events: List[ReplayEvent] = []
        self.children: List["_NodeBuilder"] = []
        self.contains_quarantined_events = False

    def add(self, event: ReplayEvent) -> None:
        self.events.append(event)
        self.start_sequence = min(self.start_sequence, event.event.sequence)
        self.end_sequence = max(self.end_sequence, event.event.sequence)
        if event.source == ReplaySource.QUARANTINED:
            self.contains_quarantined_events = True

    def build(self) -> SpanNode:
        built_children = [c.build() for c in self.children]
        any_child_quarantined = any(c.contains_quarantined_events for c in built_children)
        built_children.sort(key=lambda n: n.start_sequence)
        return SpanNode(
            span_id=self.span_id,
            start_sequence=0 if self.start_sequence == 2**64 else self.start_sequence,
            end_sequence=self.end_sequence,
            events=self.events,
            children=built_children,
            contains_quarantined_events=self.contains_quarantined_events or any_child_quarantined,
        )


class TraceReplayEngine:
    def __init__(self, committed: List[TraceEvent], quarantined: Optional[List[TraceEvent]] = None):
        self.committed = committed
        self.quarantined = quarantined if quarantined is not None else []

        raw_combined = [(ReplaySource.COMMITTED, c) for c in committed]
        raw_combined += [(ReplaySource.QUARANTINED, q) for q in self.quarantined]

        # Deterministic total ordering: sequence, timestamp, contextID, eventID.
        raw_combined.sort(
            key=lambda t: (
                t[1].sequence,
                t[1].timestamp,
                t[1].context_id,
                str(t[1].id),
            )
        )

        self._all_events = [
            ReplayEvent(source=source, event=event, replay_order=index)
            for index, (source, event) in enumerate(raw_combined)
        ]

    def snapshot(self, at: Optional[int] = None) -> ReplaySnapshot:
        max_seq = at if at is not None else (2**64 - 1)

        valid_events = [e for e in self._all_events if e.event.sequence <= max_seq]

        # Calculate gaps (events should be contiguous from 0 for a single run).
        sequence_gaps: List[SequenceGap] = []
        if valid_events:
            expected_next = 0
            for e in valid_events:
                current = e.event.sequence
                if current > expected_next:
                    sequence_gaps.append(SequenceGap(expected_next, current - 1))
                if current >= expected_next:
                    expected_next = current + 1

        span_map: Dict[str, _NodeBuilder] = {}
        root_builders: List[_NodeBuilder] = []

        # Pass 1: create builders and group events by span.
        for e in valid_events:
            span_id = e.event.span_id
            if span_id is not None:
                if span_id not in span_map:
                    span_map[span_id] = _NodeBuilder(span_id, e.event.parent_span_id)
                span_map[span_id].add(e)
            else:
                root = _NodeBuilder(None, None)
                root.add(e)
                root_builders.append(root)

        roots: List[_NodeBuilder] = []

        # Pass 2: wire up children and identify orphaned subtrees.
        for node in span_map.values():
            parent_id = node.parent_span_id
            if parent_id is not None:
                parent = span_map.get(parent_id)
                if parent is not None:
                    parent.children.append(node)
            else:
                roots.append(node)

        roots.extend(root_builders)

        # Pass 3: collect orphaned events (subtrees whose parent span is entirely missing).
        orphaned_events: List[ReplayEvent] = []
        for node in span_map.values():
            pid = node.parent_span_id
            if pid is not None and pid not in span_map:
                stack = [node]
                while stack:
                    n = stack.pop()
                    orphaned_events.extend(n.events)
                    stack.extend(n.children)

        true_roots = [b.build() for b in roots]
        true_roots.sort(key=lambda n: n.start_sequence)
        orphaned_events.sort(key=lambda e: e.replay_order)

        committed_count = 0
        quarantined_count = 0
        unique_ids = set()
        duplicate_count = 0
        for e in valid_events:
            if e.source == ReplaySource.COMMITTED:
                committed_count += 1
            else:
                quarantined_count += 1
            if e.event.id in unique_ids:
                duplicate_count += 1
            else:
                unique_ids.add(e.event.id)

        reconstructed_spans = 0
        contaminated_spans = 0

        stack = list(true_roots)
        while stack:
            node = stack.pop()
            if node.span_id is not None:
                reconstructed_spans += 1
                if node.contains_quarantined_events:
                    contaminated_spans += 1
            stack.extend(node.children)

        manifest = ReplayManifest(
            total_events=len(valid_events),
            committed_events=committed_count,
            quarantined_events=quarantined_count,
            orphaned_events=len(orphaned_events),
            duplicate_event_ids=duplicate_count,
            reconstructed_spans=reconstructed_spans,
            contaminated_spans=contaminated_spans,
            sequence_gaps=sequence_gaps,
        )

        metadata = ReplaySnapshotMetadata(
            generated_at=time.time(),
            max_sequence_included=at,
            source_counts={
                ReplaySource.COMMITTED: committed_count,
                ReplaySource.QUARANTINED: quarantined_count,
            },
        )

        return ReplaySnapshot(
            roots=true_roots,
            orphaned_events=orphaned_events,
            manifest=manifest,
            metadata=metadata,
        )


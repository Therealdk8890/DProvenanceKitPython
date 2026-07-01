"""Span-aware diff over two replay snapshots.

Where :class:`~dprovenancekit.diff.TraceDiffEngine` compares structural signatures of
flat runs, this compares reconstructed span trees: it reports span additions / removals /
reparenting / contamination changes, per-event additions / removals / modifications, and
the first point at which two timelines diverge. Events are compared by *value* (not a hash
of their encoding), so an ``Equatable``-distinct payload that happens to encode identically
still diffs as modified.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional, Tuple

from .replay import ReplayEvent, ReplaySnapshot, SpanNode

# MARK: - Change records ---------------------------------------------------------


class SpanChangeKind(Enum):
    ADDED = "added"
    REMOVED = "removed"
    REPARENTED = "reparented"
    CONTAMINATION_CHANGED = "contaminationChanged"


@dataclass(frozen=True)
class SpanChange:
    kind: SpanChangeKind
    span_id: Optional[str]
    parent_span_id: Optional[str] = None
    from_parent: Optional[str] = None
    to_parent: Optional[str] = None
    from_contaminated: Optional[bool] = None
    to_contaminated: Optional[bool] = None

    @staticmethod
    def added(span_id, parent_span_id):
        return SpanChange(SpanChangeKind.ADDED, span_id, parent_span_id=parent_span_id)

    @staticmethod
    def removed(span_id, parent_span_id):
        return SpanChange(
            SpanChangeKind.REMOVED, span_id, parent_span_id=parent_span_id
        )

    @staticmethod
    def reparented(span_id, from_parent, to_parent):
        return SpanChange(
            SpanChangeKind.REPARENTED,
            span_id,
            from_parent=from_parent,
            to_parent=to_parent,
        )

    @staticmethod
    def contamination_changed(span_id, from_, to):
        return SpanChange(
            SpanChangeKind.CONTAMINATION_CHANGED,
            span_id,
            from_contaminated=from_,
            to_contaminated=to,
        )


class EventChangeKind(Enum):
    ADDED = "added"
    REMOVED = "removed"
    MODIFIED = "modified"


@dataclass(frozen=True)
class EventChange:
    kind: EventChangeKind
    span_id: Optional[str]
    event: Optional[ReplayEvent] = None
    before: Optional[ReplayEvent] = None
    after: Optional[ReplayEvent] = None

    @staticmethod
    def added(event, span_id):
        return EventChange(EventChangeKind.ADDED, span_id, event=event)

    @staticmethod
    def removed(event, span_id):
        return EventChange(EventChangeKind.REMOVED, span_id, event=event)

    @staticmethod
    def modified(before, after, span_id):
        return EventChange(
            EventChangeKind.MODIFIED, span_id, before=before, after=after
        )


@dataclass(frozen=True)
class DivergencePoint:
    span_id: Optional[str]
    common_prefix_length: int
    divergence_sequence: int
    left_event: Optional[ReplayEvent]
    right_event: Optional[ReplayEvent]


@dataclass(frozen=True)
class DiffSummary:
    added_spans: int
    removed_spans: int
    added_events: int
    removed_events: int
    modified_events: int
    contaminated_spans: int
    divergence_points: int


@dataclass(frozen=True)
class SnapshotDiffResult:
    span_changes: List[SpanChange]
    event_changes: List[EventChange]
    divergences: List[DivergencePoint]

    @property
    def summary(self) -> DiffSummary:
        added_spans = removed_spans = contaminated = 0
        for sc in self.span_changes:
            if sc.kind == SpanChangeKind.ADDED:
                added_spans += 1
            elif sc.kind == SpanChangeKind.REMOVED:
                removed_spans += 1
            elif sc.kind == SpanChangeKind.CONTAMINATION_CHANGED:
                contaminated += 1

        added_events = removed_events = modified_events = 0
        for ec in self.event_changes:
            if ec.kind == EventChangeKind.ADDED:
                added_events += 1
            elif ec.kind == EventChangeKind.REMOVED:
                removed_events += 1
            elif ec.kind == EventChangeKind.MODIFIED:
                modified_events += 1

        return DiffSummary(
            added_spans=added_spans,
            removed_spans=removed_spans,
            added_events=added_events,
            removed_events=removed_events,
            modified_events=modified_events,
            contaminated_spans=contaminated,
            divergence_points=len(self.divergences),
        )

    @property
    def is_identical(self) -> bool:
        return not (self.span_changes or self.event_changes or self.divergences)


# MARK: - Engine -----------------------------------------------------------------

_EventIdentity = Tuple[int, str, str]  # (sequence, type_identifier, engine_name)


@dataclass(frozen=True)
class _SpanInfo:
    node: SpanNode
    parent_id: Optional[str]


class SnapshotDiffEngine:
    @staticmethod
    def _identity(e: ReplayEvent) -> _EventIdentity:
        return (e.event.sequence, e.event.payload.type_identifier, e.event.engine_name)

    @staticmethod
    def _signature(e: ReplayEvent):
        # Compare the payload *value* (events are Equatable) and the source — never a
        # hash of the encoding.
        return (e.event.payload, e.source)

    def _build_map(self, roots: List[SpanNode]) -> Dict[str, _SpanInfo]:
        result: Dict[str, _SpanInfo] = {}

        def traverse(node: SpanNode, parent_id: Optional[str]) -> None:
            if node.span_id is not None:
                result[node.span_id] = _SpanInfo(node=node, parent_id=parent_id)
                for child in node.children:
                    traverse(child, node.span_id)
            else:
                for child in node.children:
                    traverse(child, None)

        for root in roots:
            traverse(root, None)
        return result

    def _gather_root_events(self, roots: List[SpanNode]) -> List[ReplayEvent]:
        events: List[ReplayEvent] = []
        for root in roots:
            if root.span_id is None:
                events.extend(root.events)
        return events

    def diff(
        self, base: ReplaySnapshot, comparison: ReplaySnapshot
    ) -> SnapshotDiffResult:
        span_changes: List[SpanChange] = []
        event_changes: List[EventChange] = []
        divergences: List[DivergencePoint] = []

        base_map = self._build_map(base.roots)
        comp_map = self._build_map(comparison.roots)

        def diff_events(base_events, comp_events, span_id):
            common_prefix = 0
            min_len = min(len(base_events), len(comp_events))
            while common_prefix < min_len:
                b = base_events[common_prefix]
                c = comp_events[common_prefix]
                if self._identity(b) == self._identity(c) and self._signature(
                    b
                ) == self._signature(c):
                    common_prefix += 1
                else:
                    break

            if common_prefix < min_len:
                divergences.append(
                    DivergencePoint(
                        span_id=span_id,
                        common_prefix_length=common_prefix,
                        divergence_sequence=comp_events[common_prefix].event.sequence,
                        left_event=base_events[common_prefix],
                        right_event=comp_events[common_prefix],
                    )
                )

            base_dict = {self._identity(e): e for e in base_events}
            comp_dict = {self._identity(e): e for e in comp_events}

            for e in comp_events:
                ident = self._identity(e)
                b = base_dict.get(ident)
                if b is not None:
                    if self._signature(b) != self._signature(e):
                        event_changes.append(EventChange.modified(b, e, span_id))
                else:
                    event_changes.append(EventChange.added(e, span_id))

            for e in base_events:
                if self._identity(e) not in comp_dict:
                    event_changes.append(EventChange.removed(e, span_id))

        # Diff root events.
        diff_events(
            self._gather_root_events(base.roots),
            self._gather_root_events(comparison.roots),
            None,
        )

        # Diff spans.
        for span_id, comp_info in comp_map.items():
            base_info = base_map.get(span_id)
            if base_info is not None:
                if base_info.parent_id != comp_info.parent_id:
                    span_changes.append(
                        SpanChange.reparented(
                            span_id, base_info.parent_id, comp_info.parent_id
                        )
                    )
                if (
                    base_info.node.contains_quarantined_events
                    != comp_info.node.contains_quarantined_events
                ):
                    span_changes.append(
                        SpanChange.contamination_changed(
                            span_id,
                            base_info.node.contains_quarantined_events,
                            comp_info.node.contains_quarantined_events,
                        )
                    )
                if base_info.node != comp_info.node:
                    diff_events(base_info.node.events, comp_info.node.events, span_id)
            else:
                span_changes.append(SpanChange.added(span_id, comp_info.parent_id))
                diff_events([], comp_info.node.events, span_id)

        for span_id, base_info in base_map.items():
            if span_id not in comp_map:
                span_changes.append(SpanChange.removed(span_id, base_info.parent_id))
                diff_events(base_info.node.events, [], span_id)

        return SnapshotDiffResult(
            span_changes=span_changes,
            event_changes=event_changes,
            divergences=divergences,
        )

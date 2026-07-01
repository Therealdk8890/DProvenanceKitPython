"""Pure presentation models for a trace viewer (no UI framework dependency).

These mirror the value-model layer of the Swift ``DProvenanceUI`` target — stable render
identities and a flattened, collapse-aware tree — without any SwiftUI views.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Set

from .render_hints import RenderHints
from .replay import ReplayEvent, SpanNode


@dataclass(frozen=True)
class SpanViewModel:
    render_id: str
    span_id: Optional[str]
    depth: int
    is_collapsed: bool
    contains_quarantined_events: bool
    events: List[ReplayEvent]
    children: List["SpanViewModel"]

    @property
    def id(self) -> str:
        return self.render_id

    @staticmethod
    def from_node(
        node: SpanNode,
        snapshot_id: str,
        local_path_hash: str,
        depth: int,
        hints: RenderHints,
    ) -> "SpanViewModel":
        path_part = local_path_hash if local_path_hash else "root"
        render_id = f"{node.span_id if node.span_id is not None else 'root'}::{snapshot_id}::{path_part}"

        if node.span_id is not None:
            is_collapsed = node.span_id in hints.collapsed_by_default
        else:
            is_collapsed = False

        children = []
        for child in node.children:
            child_path = f"{path_part}->{child.span_id if child.span_id is not None else 'anon'}"
            children.append(
                SpanViewModel.from_node(
                    node=child,
                    snapshot_id=snapshot_id,
                    local_path_hash=child_path,
                    depth=depth + 1,
                    hints=hints,
                )
            )

        return SpanViewModel(
            render_id=render_id,
            span_id=node.span_id,
            depth=depth,
            is_collapsed=is_collapsed,
            contains_quarantined_events=node.contains_quarantined_events,
            events=node.events,
            children=children,
        )


@dataclass(frozen=True)
class FlattenedSpanNode:
    id: str
    span_id: Optional[str]
    depth: int
    is_collapsed: bool
    is_visible: bool
    has_children: bool
    contains_quarantined_events: bool
    events: List[ReplayEvent]

    @staticmethod
    def of(view_model: SpanViewModel, is_visible: bool) -> "FlattenedSpanNode":
        return FlattenedSpanNode(
            id=view_model.render_id,
            span_id=view_model.span_id,
            depth=view_model.depth,
            is_collapsed=view_model.is_collapsed,
            is_visible=is_visible,
            has_children=bool(view_model.children),
            contains_quarantined_events=view_model.contains_quarantined_events,
            events=view_model.events,
        )


def flatten_span_tree(
    roots: List[SpanViewModel], dynamic_collapsed: Set[str]
) -> List[FlattenedSpanNode]:
    result: List[FlattenedSpanNode] = []

    def traverse(node: SpanViewModel, is_visible: bool) -> None:
        result.append(FlattenedSpanNode.of(node, is_visible))
        if node.span_id is not None:
            is_collapsed = node.span_id in dynamic_collapsed
        else:
            is_collapsed = node.is_collapsed
        for child in node.children:
            traverse(child, is_visible and not is_collapsed)

    for root in roots:
        traverse(root, True)

    return result

# git-blob-rewrite

"""Flat, deterministic, UI-ready render model compiled from an alignment result."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional

from .alignment_models import AlignmentStateKind, TraceAlignmentResult


class RenderHint(Enum):
    SUCCESS = "success"
    WARNING = "warning"
    DANGER = "danger"
    INFO = "info"
    NEUTRAL = "neutral"


@dataclass(frozen=True)
class AlignmentRenderNode:
    id: str
    base_sequence: Optional[int]
    comparison_sequence: Optional[int]
    type_identifier: str
    render_hint: RenderHint
    primary_explanation: str
    has_detailed_evidence: bool
    ambiguous_alternatives: int

    @property
    def canonical_serialization(self) -> str:
        """Deterministic string serialization for snapshot hashing."""
        base = str(self.base_sequence) if self.base_sequence is not None else "nil"
        comp = str(self.comparison_sequence) if self.comparison_sequence is not None else "nil"
        return (
            f"[{base}->{comp}]|{self.type_identifier}|{self.render_hint.value}|"
            f"{self.ambiguous_alternatives}|{str(self.has_detailed_evidence).lower()}|"
            f"{self.primary_explanation}"
        )


def render_models(result: TraceAlignmentResult) -> List[AlignmentRenderNode]:
    """Compile the alignment graph into a flat, UI-ready list. Pure and deterministic."""
    nodes: List[AlignmentRenderNode] = []
    for alignment in result.alignments:
        base_seq = alignment.base_event.sequence if alignment.base_event else None
        comp_seq = alignment.comparison_event.sequence if alignment.comparison_event else None
        type_id = (
            alignment.base_event.payload.type_identifier
            if alignment.base_event
            else (
                alignment.comparison_event.payload.type_identifier
                if alignment.comparison_event
                else "unknown"
            )
        )

        kind = alignment.state.kind
        if kind == AlignmentStateKind.EXACT_MATCH:
            hint = RenderHint.SUCCESS
            explanation = "Exact match"
        elif kind == AlignmentStateKind.SEMANTIC_MATCH:
            hint = RenderHint.INFO
            conf = alignment.state.strength or 0.0
            explanation = f"Semantic match ({int(conf * 100)}%) - {alignment.explanation.primary_reason}"
        elif kind == AlignmentStateKind.REORDERED:
            hint = RenderHint.WARNING
            explanation = "Reordered"
        elif kind == AlignmentStateKind.AMBIGUOUS:
            hint = RenderHint.WARNING
            explanation = f"Ambiguous match ({alignment.state.options_count} possibilities)"
        elif kind == AlignmentStateKind.ADDED:
            hint = RenderHint.SUCCESS
            explanation = "Added in new version"
        else:  # REMOVED
            hint = RenderHint.DANGER
            explanation = "Removed in new version"

        node_id = (
            str(alignment.base_event.id)
            if alignment.base_event
            else (str(alignment.comparison_event.id) if alignment.comparison_event else str(uuid.uuid4()))
        )

        nodes.append(
            AlignmentRenderNode(
                id=node_id,
                base_sequence=base_seq,
                comparison_sequence=comp_seq,
                type_identifier=type_id,
                render_hint=hint,
                primary_explanation=explanation,
                has_detailed_evidence=bool(alignment.explanation.ranked_evidence),
                ambiguous_alternatives=len(alignment.ambiguous_candidates),
            )
        )
    return nodes

# git-blob-rewrite

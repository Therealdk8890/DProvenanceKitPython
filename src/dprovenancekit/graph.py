"""The provenance graph view and human-readable explanations."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Dict, List

from .edge import TraceEdge
from .event import TraceEvent


@dataclass(frozen=True)
class TraceGraph:
    nodes: Dict[uuid.UUID, TraceEvent]
    edges: List[TraceEdge]


@dataclass(frozen=True)
class TraceExplanation:
    target_node_id: uuid.UUID
    target_node_summary: str
    informed_by: List[str] = field(default_factory=list)
    derived_from: List[str] = field(default_factory=list)

    def formatted(self) -> str:
        lines: List[str] = [self.target_node_summary, ""]

        if self.informed_by:
            lines.append("Informed By:")
            for item in self.informed_by:
                lines.append(f"- {item}")
            lines.append("")

        if self.derived_from:
            lines.append("Derived From:")
            for item in self.derived_from:
                lines.append(f"- {item}")
            lines.append("")

        return "\n".join(lines).strip()

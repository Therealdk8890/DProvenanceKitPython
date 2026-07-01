"""Extracts human-readable findings from an alignment result."""

from __future__ import annotations

from typing import List

from .alignment_models import (
    AlignmentFinding,
    AlignmentStateKind,
    RegressionLevel,
    TraceAlignmentResult,
)
from .priority import TracePriority


class AlignmentFindingsExtractor:
    def extract(self, result: TraceAlignmentResult) -> List[AlignmentFinding]:
        findings: List[AlignmentFinding] = []

        # 1. Regression risk.
        if result.regression_risk.level != RegressionLevel.NONE:
            findings.append(AlignmentFinding.regression_risk_finding(result.regression_risk))

        # 2. Per-alignment findings, identified by the stable semantic type_identifier.
        for alignment in result.alignments:
            kind = alignment.state.kind
            if kind == AlignmentStateKind.REMOVED:
                base = alignment.base_event
                if base is not None and base.payload.priority == TracePriority.CRITICAL:
                    findings.append(
                        AlignmentFinding.critical_step_removed(base.payload.type_identifier)
                    )
            elif kind == AlignmentStateKind.ADDED:
                comp = alignment.comparison_event
                if comp is not None and comp.payload.priority == TracePriority.CRITICAL:
                    findings.append(
                        AlignmentFinding.critical_step_added(comp.payload.type_identifier)
                    )
            elif kind == AlignmentStateKind.REORDERED:
                base = alignment.base_event
                if base is not None:
                    findings.append(
                        AlignmentFinding.reordered_execution(
                            base.payload.type_identifier,
                            alignment.state.original_sequence,
                            alignment.state.new_sequence,
                        )
                    )
            elif kind == AlignmentStateKind.SEMANTIC_MATCH:
                base = alignment.base_event
                comp = alignment.comparison_event
                if base is not None and comp is not None:
                    findings.append(
                        AlignmentFinding.semantic_evolution(
                            base.payload.type_identifier, comp.payload.type_identifier
                        )
                    )
            elif kind == AlignmentStateKind.EXACT_MATCH:
                pass  # Normal, no finding needed.
            elif kind == AlignmentStateKind.AMBIGUOUS:
                base = alignment.base_event
                if base is not None:
                    findings.append(
                        AlignmentFinding.ambiguity_detected(
                            base.payload.type_identifier, alignment.state.options_count
                        )
                    )

        return findings

# git-blob-rewrite

"""Compiles findings into a plain-language narrative."""

from __future__ import annotations

from typing import List

from .alignment_models import AlignmentFinding, AlignmentFindingKind, RegressionLevel


class AlignmentNarrativeCompiler:
    def compile(self, findings: List[AlignmentFinding]) -> str:
        paragraphs: List[str] = []

        removals = [
            f.base_identifier
            for f in findings
            if f.kind == AlignmentFindingKind.CRITICAL_STEP_REMOVED
        ]
        additions = [
            f.comp_identifier
            for f in findings
            if f.kind == AlignmentFindingKind.CRITICAL_STEP_ADDED
        ]
        semantics = [
            (f.base_identifier, f.comp_identifier)
            for f in findings
            if f.kind == AlignmentFindingKind.SEMANTIC_EVOLUTION
        ]
        reorders = [
            f.base_identifier
            for f in findings
            if f.kind == AlignmentFindingKind.REORDERED_EXECUTION
        ]

        risk_level = RegressionLevel.NONE
        for f in findings:
            if (
                f.kind == AlignmentFindingKind.REGRESSION_RISK
                and f.regression_risk is not None
            ):
                risk_level = f.regression_risk.level

        if not removals and not additions and not semantics and not reorders:
            paragraphs.append(
                "This trace remained fully stable with no structural deviations."
            )
        elif not removals and not additions:
            paragraphs.append(
                "This trace remained largely stable with some structural or semantic shifts."
            )
        else:
            paragraphs.append("This trace experienced significant structural changes.")

        if removals:
            if len(removals) == 1:
                paragraphs.append(
                    f"One critical validation step ('{removals[0]}') was removed."
                )
            else:
                paragraphs.append(
                    f"{len(removals)} critical steps were removed (e.g., '{removals[0]}')."
                )

        if additions:
            if len(additions) == 1:
                paragraphs.append(
                    f"A new critical step ('{additions[0]}') was introduced."
                )
            else:
                paragraphs.append(
                    f"{len(additions)} new critical steps were introduced."
                )

        if semantics:
            for b, c in semantics:
                paragraphs.append(
                    f"The retrieval phase changed from '{b}' to '{c}' and was accepted as a semantic match."
                )

        if reorders:
            paragraphs.append(
                f"The execution order changed for {len(reorders)} step(s) "
                f"(e.g., '{reorders[0]}') without altering overall trace structure."
            )

        paragraphs.append(f"Overall regression risk: {risk_level.value.capitalize()}.")

        return "\n\n".join(paragraphs)

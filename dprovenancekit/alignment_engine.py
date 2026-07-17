"""The behavioral-equivalence engine: ``TraceAlignmentEngine``."""

from __future__ import annotations

from enum import Enum
from typing import Callable, Optional

from .alignment_config import AlignmentConfiguration
from .alignment_evidence import (
    AlignmentEvidenceCollector,
    NullEvidenceCollector,
    VerificationArtifacts,
)
from .alignment_interpreter import DefaultAlignmentInterpreter
from .alignment_matcher import DefaultTraceMatcher
from .alignment_models import (
    AlignmentStateKind,
    RegressionLevel,
    RegressionRisk,
    TraceAlignmentResult,
)
from .alignment_semantics import DefaultEquivalenceModel
from .priority import TracePriority
from .query import TraceRun


class VerificationCaptureMode(Enum):
    DISABLED = "disabled"
    EVIDENCE_ONLY = "evidenceOnly"


class TraceAlignmentEngine:
    def __init__(
        self,
        configuration: AlignmentConfiguration,
        capture_mode: VerificationCaptureMode = VerificationCaptureMode.DISABLED,
        meta_trace_callback: Optional[Callable] = None,
    ):
        self.configuration = configuration
        self.capture_mode = capture_mode
        self.meta_trace_callback = meta_trace_callback
        self._matcher = DefaultTraceMatcher(configuration)
        self._semantics = DefaultEquivalenceModel(configuration)
        self._interpreter = DefaultAlignmentInterpreter(
            configuration, meta_trace_callback
        )

    def align(
        self,
        base: TraceRun,
        comparison: TraceRun,
        minimum_priority: TracePriority = TracePriority.STRUCTURAL,
    ) -> TraceAlignmentResult:
        base_events = [e for e in base.events if e.payload.priority >= minimum_priority]
        comp_events = [
            e for e in comparison.events if e.payload.priority >= minimum_priority
        ]
        # Reorder detection compares list position, so order by the authoritative
        # ``sequence`` first. ``align()`` is public and accepts arbitrary runs (merged
        # shards, OTel-ingested, hand-built) that need not arrive sequence-sorted; without
        # this, two logically identical traces whose events merely arrive in a different
        # list order were flagged as a spurious HIGH reordering regression. A stable sort
        # is a no-op for store-backed runs, which already emerge ``ORDER BY sequence``.
        base_events.sort(key=lambda e: e.sequence)
        comp_events.sort(key=lambda e: e.sequence)

        collector = (
            AlignmentEvidenceCollector()
            if self.capture_mode == VerificationCaptureMode.EVIDENCE_ONLY
            else NullEvidenceCollector()
        )

        bindings = self._matcher.match(
            base_events, comp_events, evidence_collector=collector
        )

        def equivalence(a, b):
            return self._semantics.evaluate(a, b, evidence_collector=collector)

        alignments = self._interpreter.interpret(
            base=base_events,
            comparison=comp_events,
            bindings=bindings,
            equivalence=equivalence,
            evidence_collector=collector,
        )

        # Regression risk derives from the equivalence OUTCOME, not just the coarse
        # removed/reordered display states. A critical reasoning step degrades when it is:
        #   1. Removed outright.
        #   2. Reordered relative to another CRITICAL step — running critical steps out of
        #      order can invert a dependency (e.g. GenerateInvoice before CreateCustomer).
        #      The engine has no dependency graph, so this is critical-*order* sensitivity,
        #      not true inference; restricting to critical-vs-critical keeps a benign
        #      structural/diagnostic step moving past a stationary critical at NONE.
        #   3. Changed beyond equivalence — bound to a same-type event but with a differing
        #      payload whose match score falls below the profile's semantic_threshold.
        # Type match alone clears the matcher's bind threshold, so a changed or skipped
        # critical step is essentially never left REMOVED; it binds and is classified
        # AMBIGUOUS. Reading only removed/reordered therefore silently missed materially
        # changed or skipped critical steps (RegressionRisk.none on a tampered decision),
        # even though the equivalence model had already recorded equivalent=False.
        threshold = self.configuration.profile.semantic_threshold
        base_index_by_id = {e.id: i for i, e in enumerate(base_events)}
        comp_index_by_id_risk = {e.id: i for i, e in enumerate(comp_events)}

        removed_critical_types: list[str] = []
        changed_critical_types: list[str] = []
        # (base_idx, comp_idx, type) per matched CRITICAL pair, on the same array-index
        # basis the interpreter uses for its REORDERED findings, so the verdict can never
        # disagree with the reorder findings it summarizes.
        critical_pairs: list[tuple[int, int, str]] = []
        for a in alignments:
            b = a.base_event
            if b is None or b.payload.priority != TracePriority.CRITICAL:
                continue
            c = a.comparison_event
            if c is None:
                removed_critical_types.append(b.payload.type_identifier)
                continue
            # Identical payloads are equivalent by construction; otherwise consult the same
            # score the matcher/equivalence model used. Below the threshold => not equivalent.
            if b.payload != c.payload:
                score, _ = self.configuration.score_match(b, c)
                if score < threshold:
                    changed_critical_types.append(b.payload.type_identifier)
            if b.id in base_index_by_id and c.id in comp_index_by_id_risk:
                critical_pairs.append(
                    (base_index_by_id[b.id], comp_index_by_id_risk[c.id], b.payload.type_identifier)
                )

        reordered_critical_types: list[str] = []
        for x in critical_pairs:
            if any(x[0] != y[0] and x[0] < y[0] and x[1] > y[1] for y in critical_pairs):
                reordered_critical_types.append(x[2])

        if removed_critical_types:
            risk = RegressionRisk(
                level=RegressionLevel.HIGH,
                strength=0.95,
                reasoning=f"Critical reasoning steps removed: {', '.join(removed_critical_types)}",
            )
        elif reordered_critical_types:
            risk = RegressionRisk(
                level=RegressionLevel.HIGH,
                strength=1.0,
                reasoning=f"Critical reasoning steps reordered: {', '.join(reordered_critical_types)}",
            )
        elif changed_critical_types:
            risk = RegressionRisk(
                level=RegressionLevel.HIGH,
                strength=0.9,
                reasoning=f"Critical reasoning steps changed beyond equivalence: {', '.join(changed_critical_types)}",
            )
        else:
            risk = RegressionRisk(
                level=RegressionLevel.NONE,
                strength=1.0,
                reasoning="No critical steps removed, reordered, or materially changed.",
            )

        v_artifacts = None
        if isinstance(collector, AlignmentEvidenceCollector):
            v_artifacts = VerificationArtifacts(evidence=collector.export_evidence())

        return TraceAlignmentResult(
            base_run_id=base.run_id,
            comparison_run_id=comparison.run_id,
            profile_hash=self.configuration.profile_hash,
            engine_version="v2-causal-strict",
            alignments=alignments,
            regression_risk=risk,
            verification_artifacts=v_artifacts,
        )

    def evaluate_score(self, base, comparison):
        return self.configuration.score_match(base, comparison)

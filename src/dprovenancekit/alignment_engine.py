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
        self._interpreter = DefaultAlignmentInterpreter(configuration, meta_trace_callback)

    def align(
        self,
        base: TraceRun,
        comparison: TraceRun,
        minimum_priority: TracePriority = TracePriority.STRUCTURAL,
    ) -> TraceAlignmentResult:
        base_events = [e for e in base.events if e.payload.priority >= minimum_priority]
        comp_events = [e for e in comparison.events if e.payload.priority >= minimum_priority]

        collector = (
            AlignmentEvidenceCollector()
            if self.capture_mode == VerificationCaptureMode.EVIDENCE_ONLY
            else NullEvidenceCollector()
        )

        bindings = self._matcher.match(base_events, comp_events, evidence_collector=collector)

        def equivalence(a, b):
            return self._semantics.evaluate(a, b, evidence_collector=collector)

        alignments = self._interpreter.interpret(
            base=base_events,
            comparison=comp_events,
            bindings=bindings,
            equivalence=equivalence,
            evidence_collector=collector,
        )

        # Regression risk. Two failure modes degrade a critical reasoning step:
        #   1. Removing it outright.
        #   2. Reordering it — running critical steps out of their original order can invert
        #      a dependency (e.g. GenerateInvoice before CreateCustomer). The engine has no
        #      dependency graph, so this is critical-*order* sensitivity, not true dependency
        #      inference; it deliberately fires only on CRITICAL steps so that reordering of
        #      structural/diagnostic steps (the common, benign case) stays NONE.
        removed_critical = [
            a
            for a in alignments
            if a.state.is_removed
            and a.base_event is not None
            and a.base_event.payload.priority == TracePriority.CRITICAL
        ]
        reordered_critical = [
            a
            for a in alignments
            if a.state.kind == AlignmentStateKind.REORDERED
            and a.base_event is not None
            and a.base_event.payload.priority == TracePriority.CRITICAL
        ]
        if removed_critical:
            critical_types = ", ".join(a.base_event.payload.type_identifier for a in removed_critical)
            risk = RegressionRisk(
                level=RegressionLevel.HIGH,
                strength=0.95,
                reasoning=f"Critical reasoning steps removed: {critical_types}",
            )
        elif reordered_critical:
            reordered_types = ", ".join(
                a.base_event.payload.type_identifier for a in reordered_critical
            )
            risk = RegressionRisk(
                level=RegressionLevel.HIGH,
                strength=1.0,
                reasoning=f"Critical reasoning steps reordered: {reordered_types}",
            )
        else:
            risk = RegressionRisk(
                level=RegressionLevel.NONE,
                strength=1.0,
                reasoning="No critical steps removed or reordered.",
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


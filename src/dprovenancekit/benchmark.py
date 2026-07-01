"""Offline benchmark harness: datasets, scoring, failure diagnosis, stability.

Runs benchmark cases through the alignment engine, scores extracted findings against
ground truth with multiset matching, diagnoses each false positive / negative against a
failure taxonomy, audits explanation fidelity, and aggregates precision / recall / F1
plus a causally-ranked view of the most systemically impactful failure modes.
"""

from __future__ import annotations

import math
import time
import uuid
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Callable, Dict, List, Optional

from .alignment_engine import TraceAlignmentEngine, VerificationCaptureMode
from .alignment_findings import AlignmentFindingsExtractor
from .alignment_meta import AlignmentMetaEvent, MetaEventKind
from .alignment_models import (
    AlignmentFinding,
    AlignmentFindingKind,
    AlignmentStrengthCategory,
    DecisionTimelineEntry,
    TraceAlignmentResult,
)
from .priority import TracePriority
from .query import TraceRun
from .verification import (
    DefaultFormalizationMapBuilder,
    ExplainabilityAuditor,
    FidelityVector,
)


# MARK: - Determinism boundary ---------------------------------------------------


@dataclass(frozen=True)
class DeterministicBoundary:
    cache_isolated: bool = True
    seed_control: Optional[str] = None


@dataclass(frozen=True)
class EnvironmentContext:
    boundary: DeterministicBoundary
    iteration: int


# MARK: - Failure taxonomy -------------------------------------------------------


class SignalFailure(Enum):
    OVERSENSITIVE_MATCHER = "Oversensitive Matcher"
    THRESHOLD_MISCALIBRATION = "Threshold Miscalibration"
    SCORING_INSTABILITY = "Scoring Instability"


class ModelFailure(Enum):
    MISSING_EQUIVALENCE_RULE = "Missing Equivalence Rule"
    CANONICALIZATION_MISMATCH = "Canonicalization Mismatch"
    SEMANTIC_OVERCOLLAPSE = "Semantic Overcollapse"


class SearchFailure(Enum):
    INSUFFICIENT_CANDIDATES = "Insufficient Candidates"
    CANDIDATE_BIAS = "Candidate Bias"


class DataFailure(Enum):
    NOISE_MISCLASSIFICATION = "Noise Misclassification"
    AMBIGUOUS_GROUND_TRUTH = "Ambiguous Ground Truth"
    MISLABELED_EXPECTATION = "Mislabeled Expectation"


@dataclass(frozen=True)
class FailureSeverityProfile:
    structural_impact: float
    propagation_potential: float
    recoverability: float

    @property
    def score(self) -> float:
        w1, w2, w3 = 0.4, 0.4, 0.2
        return (w1 * self.structural_impact) + (w2 * self.propagation_potential) + (
            w3 * (1.0 - self.recoverability)
        )


@dataclass(frozen=True)
class FailureCause:
    category: str  # "signal" | "representation" | "search" | "data" | "undiagnosed"
    subtype: Optional[str] = None

    @staticmethod
    def signal(x: SignalFailure) -> "FailureCause":
        return FailureCause("signal", x.value)

    @staticmethod
    def representation(x: ModelFailure) -> "FailureCause":
        return FailureCause("representation", x.value)

    @staticmethod
    def search(x: SearchFailure) -> "FailureCause":
        return FailureCause("search", x.value)

    @staticmethod
    def data(x: DataFailure) -> "FailureCause":
        return FailureCause("data", x.value)

    @staticmethod
    def undiagnosed() -> "FailureCause":
        return FailureCause("undiagnosed")

    @property
    def label(self) -> str:
        if self.category == "signal":
            return f"Signal: {self.subtype}"
        if self.category == "representation":
            return f"Representation: {self.subtype}"
        if self.category == "search":
            return f"Search: {self.subtype}"
        if self.category == "data":
            return f"Data: {self.subtype}"
        return "Undiagnosed"

    @property
    def severity_profile(self) -> FailureSeverityProfile:
        c, s = self.category, self.subtype
        if c == "representation" and s in (
            ModelFailure.MISSING_EQUIVALENCE_RULE.value,
            ModelFailure.SEMANTIC_OVERCOLLAPSE.value,
        ):
            return FailureSeverityProfile(1.0, 0.8, 0.0)
        if c == "search" and s == SearchFailure.INSUFFICIENT_CANDIDATES.value:
            return FailureSeverityProfile(0.8, 0.6, 0.2)
        if c == "signal" and s == SignalFailure.SCORING_INSTABILITY.value:
            return FailureSeverityProfile(0.6, 0.9, 0.3)
        if (c == "signal" and s == SignalFailure.THRESHOLD_MISCALIBRATION.value) or (
            c == "search" and s == SearchFailure.CANDIDATE_BIAS.value
        ):
            return FailureSeverityProfile(0.6, 0.5, 0.4)
        if c == "signal" and s == SignalFailure.OVERSENSITIVE_MATCHER.value:
            return FailureSeverityProfile(0.5, 0.4, 0.5)
        if c == "representation" and s == ModelFailure.CANONICALIZATION_MISMATCH.value:
            return FailureSeverityProfile(0.4, 0.3, 0.6)
        if c == "data":
            return FailureSeverityProfile(0.0, 0.0, 1.0)
        return FailureSeverityProfile(0.1, 0.1, 0.5)  # undiagnosed

    @property
    def severity(self) -> float:
        return self.severity_profile.score


@dataclass(frozen=True)
class DiagnosedFailure:
    finding: AlignmentFinding
    is_false_positive: bool
    is_engine_error: bool
    hypothesized_cause: FailureCause
    diagnosis_confidence: float
    reason: str
    evidence_ids: List[uuid.UUID]

    def resolved_evidence(self, timeline: List[DecisionTimelineEntry]) -> List[DecisionTimelineEntry]:
        ids = set(self.evidence_ids)
        return [e for e in timeline if e.id in ids]


def make_diagnosed_failure(
    finding, is_false_positive, is_engine_error, hypothesized_cause, diagnosis_confidence, reason, evidence_ids
) -> DiagnosedFailure:
    # EVIDENCE RESTRICTION: No evidence, no claim.
    if not evidence_ids and hypothesized_cause != FailureCause.undiagnosed():
        return DiagnosedFailure(
            finding=finding,
            is_false_positive=is_false_positive,
            is_engine_error=is_engine_error,
            hypothesized_cause=FailureCause.undiagnosed(),
            diagnosis_confidence=0.0,
            reason="Unverifiable hypothesis (no evidence trace). Reverted to undiagnosed.",
            evidence_ids=list(evidence_ids),
        )
    return DiagnosedFailure(
        finding=finding,
        is_false_positive=is_false_positive,
        is_engine_error=is_engine_error,
        hypothesized_cause=hypothesized_cause,
        diagnosis_confidence=diagnosis_confidence,
        reason=reason,
        evidence_ids=list(evidence_ids),
    )


# MARK: - Benchmark models -------------------------------------------------------


@dataclass(frozen=True)
class ExpectedFinding:
    finding: AlignmentFinding
    expected_confidence: float = 1.0


@dataclass(frozen=True)
class BenchmarkCase:
    name: str
    description: str
    base_run: TraceRun
    comparison_run: TraceRun
    expected_findings: List[ExpectedFinding]
    id: str = field(default_factory=lambda: str(uuid.uuid4()))


@dataclass(frozen=True)
class BenchmarkDataset:
    name: str
    description: str
    cases: List[BenchmarkCase]


@dataclass(frozen=True)
class CategoryMetrics:
    true_positives: int
    false_positives: int
    false_negatives: int

    @property
    def precision(self) -> float:
        total = self.true_positives + self.false_positives
        return self.true_positives / total if total > 0 else 1.0

    @property
    def recall(self) -> float:
        total = self.true_positives + self.false_negatives
        return self.true_positives / total if total > 0 else 1.0

    @property
    def f1_score(self) -> float:
        p, r = self.precision, self.recall
        return 2 * (p * r) / (p + r) if (p + r) > 0 else 0.0


@dataclass(frozen=True)
class BenchmarkCaseResult:
    benchmark_case: BenchmarkCase
    run_time_ms: float
    actual_findings: List[AlignmentFinding]
    true_positives: List[AlignmentFinding]
    false_positives: List[AlignmentFinding]
    false_negatives: List[ExpectedFinding]
    diagnoses: List[DiagnosedFailure]
    fidelity_score: FidelityVector
    alignment_result: TraceAlignmentResult
    timeline: List[DecisionTimelineEntry]

    @property
    def passed(self) -> bool:
        return not self.false_positives and not self.false_negatives


@dataclass(frozen=True)
class CausalRank:
    cause: FailureCause
    frequency: int
    average_confidence: float
    raw_impact_score: float
    fractional_impact: float
    z_score_impact: float


@dataclass(frozen=True)
class BenchmarkReport:
    dataset_name: str
    case_results: List[BenchmarkCaseResult]
    total_cases: int
    passed_cases: int
    average_run_time_ms: float
    p95_run_time_ms: float
    global_metrics: CategoryMetrics
    stratified_metrics: Dict[str, CategoryMetrics]
    average_fidelity_score: float

    @property
    def causal_ranking(self) -> List[CausalRank]:
        groups: Dict[FailureCause, List[DiagnosedFailure]] = {}
        for case_result in self.case_results:
            for diagnosis in case_result.diagnoses:
                groups.setdefault(diagnosis.hypothesized_cause, []).append(diagnosis)

        raw_ranks = []
        for cause, failures in groups.items():
            freq = len(failures)
            avg_conf = sum(f.diagnosis_confidence for f in failures) / freq
            raw_impact = freq * cause.severity * avg_conf
            raw_ranks.append((cause, freq, avg_conf, raw_impact))

        total_impact = sum(r[3] for r in raw_ranks)
        mean_impact = (total_impact / len(raw_ranks)) if raw_ranks else 0.0
        variance = (
            sum((r[3] - mean_impact) ** 2 for r in raw_ranks) / len(raw_ranks)
            if raw_ranks
            else 0.0
        )
        stdev = math.sqrt(variance)

        ranks = []
        for cause, freq, avg_conf, raw_impact in raw_ranks:
            fractional = (raw_impact / total_impact) if total_impact > 0 else 0.0
            z_score = ((raw_impact - mean_impact) / stdev) if stdev > 0 else 0.0
            ranks.append(
                CausalRank(
                    cause=cause,
                    frequency=freq,
                    average_confidence=avg_conf,
                    raw_impact_score=raw_impact,
                    fractional_impact=fractional,
                    z_score_impact=z_score,
                )
            )
        ranks.sort(key=lambda r: r.raw_impact_score, reverse=True)
        return ranks

    def compare(self, baseline: "BenchmarkReport") -> "BenchmarkDeltaReport":
        return BenchmarkDeltaReport(self, baseline)


@dataclass(frozen=True)
class BenchmarkStabilityReport:
    iterations: int
    reports: List[BenchmarkReport]
    boundary: DeterministicBoundary = DeterministicBoundary()

    @property
    def mean_precision(self) -> float:
        return sum(r.global_metrics.precision for r in self.reports) / self.iterations

    @property
    def mean_recall(self) -> float:
        return sum(r.global_metrics.recall for r in self.reports) / self.iterations

    @property
    def mean_f1(self) -> float:
        return sum(r.global_metrics.f1_score for r in self.reports) / self.iterations

    @property
    def precision_variance(self) -> float:
        mean = self.mean_precision
        return sum((r.global_metrics.precision - mean) ** 2 for r in self.reports) / self.iterations

    @property
    def f1_variance(self) -> float:
        mean = self.mean_f1
        return sum((r.global_metrics.f1_score - mean) ** 2 for r in self.reports) / self.iterations

    @property
    def drift_fingerprint(self) -> str:
        if self.f1_variance < 0.0001:
            return "Stable: No significant drift"
        if self.precision_variance > self.f1_variance:
            return "Unstable: Precision fluctuates (oversensitive matcher boundary)"
        return "Unstable: Recall fluctuates (inconsistent search space exploration)"


@dataclass(frozen=True)
class CategoryDeltaMetrics:
    precision_delta: float
    recall_delta: float
    f1_delta: float

    @staticmethod
    def of(current: CategoryMetrics, baseline: CategoryMetrics) -> "CategoryDeltaMetrics":
        return CategoryDeltaMetrics(
            precision_delta=current.precision - baseline.precision,
            recall_delta=current.recall - baseline.recall,
            f1_delta=current.f1_score - baseline.f1_score,
        )


@dataclass(frozen=True)
class BenchmarkDeltaReport:
    current_report: BenchmarkReport
    baseline_report: BenchmarkReport
    global_delta: CategoryDeltaMetrics = field(init=False)
    stratified_deltas: Dict[str, CategoryDeltaMetrics] = field(init=False)
    runtime_delta_ms: float = field(init=False)

    def __init__(self, current: BenchmarkReport, baseline: BenchmarkReport):
        object.__setattr__(self, "current_report", current)
        object.__setattr__(self, "baseline_report", baseline)
        object.__setattr__(
            self, "global_delta", CategoryDeltaMetrics.of(current.global_metrics, baseline.global_metrics)
        )
        empty = CategoryMetrics(0, 0, 0)
        strats: Dict[str, CategoryDeltaMetrics] = {}
        for cat in set(current.stratified_metrics) | set(baseline.stratified_metrics):
            curr = current.stratified_metrics.get(cat, empty)
            base = baseline.stratified_metrics.get(cat, empty)
            strats[cat] = CategoryDeltaMetrics.of(curr, base)
        object.__setattr__(self, "stratified_deltas", strats)
        object.__setattr__(
            self, "runtime_delta_ms", current.average_run_time_ms - baseline.average_run_time_ms
        )


# MARK: - Failure diagnoser ------------------------------------------------------


class BenchmarkFailureDiagnoser:
    def diagnose(
        self,
        false_positives: List[AlignmentFinding],
        false_negatives: List[ExpectedFinding],
        timeline: List[DecisionTimelineEntry],
        alignment_result: TraceAlignmentResult,
    ) -> List[DiagnosedFailure]:
        diagnoses: List[DiagnosedFailure] = []

        def comparison_sequences(for_type: str):
            result = set()
            for a in alignment_result.alignments:
                comp = a.comparison_event
                if comp is not None and comp.payload.type_identifier == for_type:
                    result.add(comp.sequence)
            return result

        # Diagnose false negatives (engine missed something expected).
        for expected in false_negatives:
            is_engine_error = expected.expected_confidence >= 0.8
            best_cause = FailureCause.undiagnosed()
            confidence = 0.0
            reason = "Could not infer cause from execution trace."
            evidence: List[uuid.UUID] = []

            f = expected.finding
            if f.kind == AlignmentFindingKind.SEMANTIC_EVOLUTION:
                comp_id = f.comp_identifier
                comp_seqs = comparison_sequences(comp_id)

                rejections = []
                for e in timeline:
                    if e.strength_category != AlignmentStrengthCategory.REJECTED or e.meta_event is None:
                        continue
                    meta = e.meta_event
                    if meta.kind in (
                        MetaEventKind.CANDIDATE_EVICTED,
                        MetaEventKind.AMBIGUITY_THRESHOLD_MET,
                        MetaEventKind.EVALUATED_PAIR,
                    ):
                        if meta.comp_sequence in comp_seqs:
                            rejections.append(e)

                if rejections:
                    best_cause = FailureCause.signal(SignalFailure.THRESHOLD_MISCALIBRATION)
                    confidence = 0.85
                    reason = (
                        "A semantic candidate was found but evicted, suggesting the "
                        "threshold is slightly too strict."
                    )
                    evidence.append(rejections[0].id)
                else:
                    evaluations = []
                    for e in timeline:
                        if e.meta_event is None:
                            continue
                        if e.meta_event.kind == MetaEventKind.EVALUATED_PAIR and e.meta_event.comp_sequence in comp_seqs:
                            evaluations.append(e)
                    if not evaluations:
                        best_cause = FailureCause.search(SearchFailure.INSUFFICIENT_CANDIDATES)
                        confidence = 0.7
                        reason = (
                            f"No candidates were even evaluated for {comp_id}. "
                            "Search space failed to generate pairs."
                        )
                    else:
                        best_cause = FailureCause.representation(ModelFailure.MISSING_EQUIVALENCE_RULE)
                        confidence = 0.6
                        reason = (
                            f"Evaluated candidates for {comp_id} scored zero, meaning the "
                            "evaluator lacked equivalence rules."
                        )
                        evidence.extend(e.id for e in evaluations)

            elif f.kind == AlignmentFindingKind.CRITICAL_STEP_REMOVED:
                base_id = f.base_identifier
                matches = [
                    a for a in alignment_result.alignments
                    if a.state.is_semantic_match or a.state.is_exact_match
                ]
                if any(m.base_event is not None and m.base_event.payload.type_identifier == base_id for m in matches):
                    best_cause = FailureCause.signal(SignalFailure.OVERSENSITIVE_MATCHER)
                    confidence = 0.9
                    reason = (
                        f"Engine aggressively aligned {base_id} when it should have been "
                        "considered removed."
                    )
                else:
                    best_cause = FailureCause.representation(ModelFailure.CANONICALIZATION_MISMATCH)
                    confidence = 0.5
                    reason = (
                        "Step was removed, but it was not flagged as critical. Priority "
                        "definition might be mismatched."
                    )

            diagnoses.append(
                make_diagnosed_failure(
                    finding=f,
                    is_false_positive=False,
                    is_engine_error=is_engine_error,
                    hypothesized_cause=best_cause,
                    diagnosis_confidence=confidence,
                    reason=reason,
                    evidence_ids=evidence,
                )
            )

        # Diagnose false positives (engine hallucinates a finding).
        for actual in false_positives:
            best_cause = FailureCause.undiagnosed()
            confidence = 0.0
            reason = "Unexpected finding with no clear causal misstep."
            evidence = []

            if actual.kind == AlignmentFindingKind.SEMANTIC_EVOLUTION:
                comp_id = actual.comp_identifier
                comp_seqs = comparison_sequences(comp_id)
                evals = []
                for e in timeline:
                    if e.strength_category == AlignmentStrengthCategory.REJECTED or e.meta_event is None:
                        continue
                    meta = e.meta_event
                    if meta.kind in (MetaEventKind.EVALUATED_PAIR, MetaEventKind.AMBIGUITY_THRESHOLD_MET):
                        if meta.comp_sequence in comp_seqs:
                            evals.append(e)
                if evals:
                    best_cause = FailureCause.signal(SignalFailure.OVERSENSITIVE_MATCHER)
                    confidence = 0.8
                    reason = "Semantic match passed threshold, but ground truth didn't expect it."
                    evidence.extend(e.id for e in evals)
                else:
                    best_cause = FailureCause.data(DataFailure.NOISE_MISCLASSIFICATION)
                    confidence = 0.6
                    reason = "Unrelated event was coerced into a match."

            elif actual.kind == AlignmentFindingKind.REORDERED_EXECUTION:
                new_seq = actual.new_sequence
                evals = []
                for e in timeline:
                    if e.meta_event is None:
                        continue
                    if e.meta_event.kind == MetaEventKind.EVALUATED_PAIR and e.meta_event.comp_sequence == new_seq:
                        evals.append(e)
                best_cause = FailureCause.signal(SignalFailure.SCORING_INSTABILITY)
                confidence = 0.5 if not evals else 0.7
                reason = (
                    "Engine reported a reordering the ground truth did not expect; "
                    "positional scoring may be over-firing."
                )
                evidence.extend(e.id for e in evals)

            elif actual.kind == AlignmentFindingKind.AMBIGUITY_DETECTED:
                ambiguities = [
                    e for e in timeline
                    if e.meta_event is not None and e.meta_event.kind == MetaEventKind.AMBIGUITY_THRESHOLD_MET
                ]
                best_cause = FailureCause.signal(SignalFailure.OVERSENSITIVE_MATCHER)
                confidence = 0.5 if not ambiguities else 0.65
                reason = (
                    "Engine flagged ambiguity the ground truth did not expect; the "
                    "ambiguity threshold may be too permissive."
                )
                evidence.extend(e.id for e in ambiguities)
            else:
                best_cause = FailureCause.undiagnosed()
                confidence = 0.0
                reason = "Not enough heuristics implemented for this FP."

            diagnoses.append(
                make_diagnosed_failure(
                    finding=actual,
                    is_false_positive=True,
                    is_engine_error=True,
                    hypothesized_cause=best_cause,
                    diagnosis_confidence=confidence,
                    reason=reason,
                    evidence_ids=evidence,
                )
            )

        return diagnoses


# MARK: - Runner -----------------------------------------------------------------

EngineFactory = Callable[[Callable], TraceAlignmentEngine]
ContextualEngineFactory = Callable[[EnvironmentContext, Callable], TraceAlignmentEngine]


def _match_key(finding: AlignmentFinding) -> AlignmentFinding:
    """Identity of a finding for ground-truth matching.

    A finding's ``reasoning`` is human-facing explanatory prose, not part of its semantic
    identity: ``RegressionRisk(HIGH, 0.95, "Critical step removed: decision")`` is the *same*
    finding the ground truth means by ``RegressionRisk(HIGH, 0.95, "")``. Comparing the prose
    would otherwise turn a correct detection into a simultaneous false positive + false
    negative. Level and strength remain significant; only the free-text reasoning is erased.
    """
    if finding.kind == AlignmentFindingKind.REGRESSION_RISK and finding.regression_risk is not None:
        return replace(finding, regression_risk=replace(finding.regression_risk, reasoning=""))
    return finding


class BenchmarkRunner:
    def run_repeated_evaluation(
        self,
        dataset: BenchmarkDataset,
        iterations: int,
        engine_factory: ContextualEngineFactory,
        boundary: DeterministicBoundary = DeterministicBoundary(),
    ) -> BenchmarkStabilityReport:
        reports = []
        for i in range(iterations):
            context = EnvironmentContext(boundary=boundary, iteration=i)
            report = self.run(dataset, lambda cb, ctx=context: engine_factory(ctx, cb))
            reports.append(report)
        return BenchmarkStabilityReport(iterations=iterations, reports=reports, boundary=boundary)

    def run(self, dataset: BenchmarkDataset, engine_factory: EngineFactory) -> BenchmarkReport:
        case_results: List[BenchmarkCaseResult] = []
        run_times: List[float] = []

        global_tp = global_fp = global_fn = 0
        category_tp: Dict[str, int] = {}
        category_fp: Dict[str, int] = {}
        category_fn: Dict[str, int] = {}

        for b_case in dataset.cases:
            collected_events: List = []
            callback = collected_events.append

            engine = engine_factory(callback)

            start = time.perf_counter()
            alignment_result = engine.align(
                base=b_case.base_run,
                comparison=b_case.comparison_run,
                minimum_priority=TracePriority.DIAGNOSTIC,
            )
            duration = (time.perf_counter() - start) * 1000.0
            run_times.append(duration)

            actual_findings = AlignmentFindingsExtractor().extract(alignment_result)

            timeline = [self._timeline_entry(ev) for ev in collected_events]

            # Multiset matching: consume actual findings as expectations are matched.
            # Matching is on semantic identity (_match_key), not human-facing reasoning prose.
            true_positives: List[AlignmentFinding] = []
            false_positives: List[AlignmentFinding] = []
            false_negatives: List[ExpectedFinding] = []
            available_actual = list(actual_findings)

            for expected in b_case.expected_findings:
                expected_key = _match_key(expected.finding)
                match = next(
                    (a for a in available_actual if _match_key(a) == expected_key), None
                )
                if match is not None:
                    available_actual.remove(match)
                    true_positives.append(expected.finding)
                    category_tp[expected.finding.category_name] = category_tp.get(expected.finding.category_name, 0) + 1
                    global_tp += 1
                else:
                    false_negatives.append(expected)
                    category_fn[expected.finding.category_name] = category_fn.get(expected.finding.category_name, 0) + 1
                    global_fn += 1

            for actual in available_actual:
                false_positives.append(actual)
                category_fp[actual.category_name] = category_fp.get(actual.category_name, 0) + 1
                global_fp += 1

            diagnoses = BenchmarkFailureDiagnoser().diagnose(
                false_positives=false_positives,
                false_negatives=false_negatives,
                timeline=timeline,
                alignment_result=alignment_result,
            )

            auditor = ExplainabilityAuditor()
            if not actual_findings:
                fidelity = (
                    FidelityVector(1, 1, 1, 1)
                    if not false_negatives
                    else FidelityVector(0, 0, 0, 0)
                )
            elif alignment_result.verification_artifacts is not None:
                builder = DefaultFormalizationMapBuilder()
                fidelity = auditor.audit(builder.build(alignment_result.verification_artifacts.evidence))
            else:
                fidelity = FidelityVector(0, 0, 0, 0)

            case_results.append(
                BenchmarkCaseResult(
                    benchmark_case=b_case,
                    run_time_ms=duration,
                    actual_findings=actual_findings,
                    true_positives=true_positives,
                    false_positives=false_positives,
                    false_negatives=false_negatives,
                    diagnoses=diagnoses,
                    fidelity_score=fidelity,
                    alignment_result=alignment_result,
                    timeline=timeline,
                )
            )

        sorted_runtimes = sorted(run_times)
        avg_runtime = sum(sorted_runtimes) / len(sorted_runtimes) if sorted_runtimes else 0.0
        if sorted_runtimes:
            p95_index = max(0, math.ceil(len(sorted_runtimes) * 0.95) - 1)
            p95_runtime = sorted_runtimes[min(p95_index, len(sorted_runtimes) - 1)]
        else:
            p95_runtime = 0.0

        avg_fidelity = (
            sum(c.fidelity_score.overall_score for c in case_results) / len(case_results)
            if case_results
            else 1.0
        )

        global_metrics = CategoryMetrics(global_tp, global_fp, global_fn)

        stratified: Dict[str, CategoryMetrics] = {}
        for cat in set(category_tp) | set(category_fp) | set(category_fn):
            stratified[cat] = CategoryMetrics(
                category_tp.get(cat, 0), category_fp.get(cat, 0), category_fn.get(cat, 0)
            )

        passed_cases = sum(1 for c in case_results if c.passed)

        return BenchmarkReport(
            dataset_name=dataset.name,
            case_results=case_results,
            total_cases=len(case_results),
            passed_cases=passed_cases,
            average_run_time_ms=avg_runtime,
            p95_run_time_ms=p95_runtime,
            global_metrics=global_metrics,
            stratified_metrics=stratified,
            average_fidelity_score=avg_fidelity,
        )

    @staticmethod
    def _timeline_entry(event) -> DecisionTimelineEntry:
        meta: AlignmentMetaEvent = event.payload
        category = None
        if meta.kind == MetaEventKind.EVALUATED_PAIR:
            title = f"Evaluated Base:{meta.base_sequence} → Comp:{meta.comp_sequence}"
            detail = "Calculated heuristic alignment score."
            category = AlignmentStrengthCategory.from_strength(meta.score)
        elif meta.kind == MetaEventKind.AMBIGUITY_THRESHOLD_MET:
            title = "Ambiguity Threshold Exceeded"
            detail = f"Comparison event {meta.comp_sequence} hit ambiguity threshold."
            category = AlignmentStrengthCategory.from_strength(meta.score)
        elif meta.kind == MetaEventKind.CANDIDATE_EVICTED:
            title = f"Rejected Comp:{meta.comp_sequence}"
            detail = f"Reason: {meta.reason}"
            category = AlignmentStrengthCategory.REJECTED
        else:  # REGRESSION_DETECTED
            title = f"Regression Risk: {str(meta.level).capitalize()}"
            detail = meta.reasoning or ""

        return DecisionTimelineEntry(
            id=event.id,
            timestamp=event.timestamp,
            title=title,
            detail=detail,
            strength_category=category,
            meta_event=meta,
        )

# git-blob-rewrite

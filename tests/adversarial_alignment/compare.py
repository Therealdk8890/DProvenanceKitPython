"""Compare production TraceAlignmentEngine pairings vs optimal assignment."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import List, Optional, Sequence

from dprovenancekit import (
    AlignmentConfiguration,
    RegressionLevel,
    TraceAlignmentEngine,
    TracePriority,
)
from dprovenancekit.alignment_evidence import NullEvidenceCollector
from dprovenancekit.alignment_interpreter import DefaultAlignmentInterpreter
from dprovenancekit.alignment_models import RegressionRisk
from dprovenancekit.alignment_semantics import DefaultEquivalenceModel
from dprovenancekit.priority import TracePriority as TP

from .optimal_assignment import (
    binding_pair_set,
    greedy_bindings,
    optimal_bindings,
    total_score,
)


@dataclass
class CaseMetrics:
    name: str
    category: str
    pairing_disagrees: bool
    production_pair_count: int
    optimal_pair_count: int
    production_score_sum: float
    optimal_score_sum: float
    production_risk: str
    optimal_risk: str
    verdict_flips: bool
    high_none_flip: bool
    notes: str = ""


@dataclass
class SuiteReport:
    cases: List[CaseMetrics] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.cases)

    @property
    def disagreement_rate(self) -> float:
        if not self.cases:
            return 0.0
        return sum(1 for c in self.cases if c.pairing_disagrees) / self.n

    @property
    def verdict_flip_rate(self) -> float:
        if not self.cases:
            return 0.0
        return sum(1 for c in self.cases if c.verdict_flips) / self.n

    @property
    def high_none_flip_rate(self) -> float:
        if not self.cases:
            return 0.0
        return sum(1 for c in self.cases if c.high_none_flip) / self.n

    def categorized_failures(self) -> dict:
        out: dict = {}
        for c in self.cases:
            if not (c.pairing_disagrees or c.verdict_flips):
                continue
            out.setdefault(c.category, []).append(
                {
                    "name": c.name,
                    "pairing_disagrees": c.pairing_disagrees,
                    "verdict_flips": c.verdict_flips,
                    "high_none_flip": c.high_none_flip,
                    "production_risk": c.production_risk,
                    "optimal_risk": c.optimal_risk,
                }
            )
        return out

    def to_dict(self) -> dict:
        return {
            "n_cases": self.n,
            "disagreement_rate": self.disagreement_rate,
            "verdict_flip_rate": self.verdict_flip_rate,
            "high_none_flip_rate": self.high_none_flip_rate,
            "categorized_failures": self.categorized_failures(),
            "cases": [asdict(c) for c in self.cases],
        }

    def dumps(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)


def _filter_events(events, minimum_priority: TracePriority):
    ev = [e for e in events if e.payload.priority >= minimum_priority]
    ev.sort(key=lambda e: e.sequence)
    return ev


def risk_from_alignments(configuration, alignments, base_events, comp_events) -> RegressionRisk:
    """Mirror TraceAlignmentEngine regression-risk derivation (do not modify engine)."""
    threshold = configuration.profile.semantic_threshold
    base_index_by_id = {e.id: i for i, e in enumerate(base_events)}
    comp_index_by_id = {e.id: i for i, e in enumerate(comp_events)}

    removed_critical_types: list[str] = []
    changed_critical_types: list[str] = []
    critical_pairs: list[tuple[int, int, str]] = []
    for a in alignments:
        b = a.base_event
        if b is None or b.payload.priority != TP.CRITICAL:
            continue
        c = a.comparison_event
        if c is None:
            removed_critical_types.append(b.payload.type_identifier)
            continue
        if b.payload != c.payload:
            score, _ = configuration.score_match(b, c)
            if score < threshold:
                changed_critical_types.append(b.payload.type_identifier)
        if b.id in base_index_by_id and c.id in comp_index_by_id:
            critical_pairs.append(
                (base_index_by_id[b.id], comp_index_by_id[c.id], b.payload.type_identifier)
            )

    reordered_critical_types: list[str] = []
    for x in critical_pairs:
        if any(x[0] != y[0] and x[0] < y[0] and x[1] > y[1] for y in critical_pairs):
            reordered_critical_types.append(x[2])

    if removed_critical_types:
        return RegressionRisk(
            level=RegressionLevel.HIGH,
            strength=0.95,
            reasoning=f"Critical reasoning steps removed: {', '.join(removed_critical_types)}",
        )
    if reordered_critical_types:
        return RegressionRisk(
            level=RegressionLevel.HIGH,
            strength=1.0,
            reasoning=f"Critical reasoning steps reordered: {', '.join(reordered_critical_types)}",
        )
    if changed_critical_types:
        return RegressionRisk(
            level=RegressionLevel.HIGH,
            strength=0.9,
            reasoning=(
                "Critical reasoning steps changed beyond equivalence: "
                f"{', '.join(changed_critical_types)}"
            ),
        )
    return RegressionRisk(
        level=RegressionLevel.NONE,
        strength=1.0,
        reasoning="No critical steps removed, reordered, or materially changed.",
    )


def align_with_bindings(configuration, base_events, comp_events, bindings):
    interpreter = DefaultAlignmentInterpreter(configuration)
    semantics = DefaultEquivalenceModel(configuration)

    def equivalence(a, b):
        return semantics.evaluate(a, b, evidence_collector=NullEvidenceCollector())

    alignments = interpreter.interpret(
        base=base_events,
        comparison=comp_events,
        bindings=bindings,
        equivalence=equivalence,
        evidence_collector=NullEvidenceCollector(),
    )
    risk = risk_from_alignments(configuration, alignments, base_events, comp_events)
    return alignments, risk


def evaluate_case(
    configuration: AlignmentConfiguration,
    name: str,
    category: str,
    base,
    comparison,
    *,
    minimum_priority: TracePriority = TracePriority.STRUCTURAL,
    notes: str = "",
) -> CaseMetrics:
    base_events = _filter_events(base.events, minimum_priority)
    comp_events = _filter_events(comparison.events, minimum_priority)

    # Production path (full engine) for authoritative production risk.
    production_result = TraceAlignmentEngine(configuration).align(
        base, comparison, minimum_priority=minimum_priority
    )
    prod_bindings = greedy_bindings(configuration, base_events, comp_events)
    opt_bindings = optimal_bindings(configuration, base_events, comp_events)

    _, opt_risk = align_with_bindings(
        configuration, base_events, comp_events, opt_bindings
    )

    prod_pairs = binding_pair_set(prod_bindings)
    opt_pairs = binding_pair_set(opt_bindings)
    pairing_disagrees = prod_pairs != opt_pairs

    prod_level = production_result.regression_risk.level
    opt_level = opt_risk.level
    verdict_flips = prod_level != opt_level
    high_none = {RegressionLevel.HIGH, RegressionLevel.NONE}
    high_none_flip = verdict_flips and {prod_level, opt_level} == high_none

    return CaseMetrics(
        name=name,
        category=category,
        pairing_disagrees=pairing_disagrees,
        production_pair_count=len(prod_bindings),
        optimal_pair_count=len(opt_bindings),
        production_score_sum=total_score(prod_bindings),
        optimal_score_sum=total_score(opt_bindings),
        production_risk=prod_level.value,
        optimal_risk=opt_level.value,
        verdict_flips=verdict_flips,
        high_none_flip=high_none_flip,
        notes=notes,
    )


def run_suite(configuration, cases: Sequence) -> SuiteReport:
    report = SuiteReport()
    for case in cases:
        report.cases.append(
            evaluate_case(
                configuration,
                case.name,
                case.category,
                case.base,
                case.comparison,
                notes=getattr(case, "notes", "") or "",
            )
        )
    return report

"""Adversarial alignment suite: production greedy matcher vs optimal assignment.

Does NOT modify the production TraceAlignmentEngine / DefaultTraceMatcher.
Answers: how often does the production matcher disagree with a globally optimal
bipartite assignment, and when it disagrees, can that flip the regression verdict
(especially HIGH vs none for remove/reorder/changed criticals)?
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dprovenancekit import (
    AlignmentConfiguration,
    AlignmentMode,
    AlignmentProfile,
    AlignmentStrategy,
    AnyEquivalenceEvaluator,
    RegressionLevel,
    TraceAlignmentEngine,
)

from tests.adversarial_alignment.compare import (
    evaluate_case,
    run_suite,
)
from tests.adversarial_alignment.generators import (
    all_generator_cases,
    greedy_trap_assignment,
    semantic_evaluator_disagree_hooks,
)
from tests.adversarial_alignment.optimal_assignment import (
    greedy_bindings,
    hungarian_maximize,
    optimal_bindings,
    total_score,
)


# Budgets: pairing disagreements are informative (PASS with metrics); unexpected
# HIGH↔none flips under ExactEquality_v1 fail hard unless documented.
PAIRING_DISAGREEMENT_BUDGET = 1.0  # allow all pairing disagreements
VERDICT_FLIP_BUDGET = 0.25  # allow some MEDIUM/LOW nuance; HIGH↔none tracked separately
UNEXPECTED_HIGH_NONE_FLIP_BUDGET = 0.0  # hard fail on ExactEquality HIGH↔none flips


def _exact_equality_config(profile: AlignmentProfile = None) -> AlignmentConfiguration:
    profile = profile or AlignmentProfile.strict_audit_v1
    evaluator = AnyEquivalenceEvaluator(
        evaluator_identifier="ExactEquality_v1",
        evaluator=lambda a, b: 1.0 if a == b else 0.0,
    )
    return AlignmentConfiguration(profile, evaluator)


def _graded_trap_config() -> AlignmentConfiguration:
    """Payload-graded evaluator that realizes the classic greedy trap."""

    # Intended score matrix (after type weight in strict_audit: type 0.5 + payload*0.5):
    #   b0-c0: payload 0.8 → total 0.5+0.4 = 0.9
    #   b0-c1: payload 0.6 → total 0.5+0.3 = 0.8
    #   b1-c0: payload 0.7 → total 0.5+0.35 = 0.85
    #   b1-c1: payload 0.0 → total 0.5 (still >= 0.4 bind floor)
    # Wait — type always matches so type contrib is always 0.5. For b1-c1 we need
    # below-threshold OR low enough that optimal prefers (0,1)+(1,0).
    # Optimal wants max weight: (0,1)+(1,0) = 0.8+0.85 = 1.65 vs greedy (0,0)=0.9 then
    # (1,1)=0.5 → 1.4. So greedy and optimal both assign both — but different pairs.
    grades = {
        ("b0", "c0"): 0.8,
        ("b0", "c1"): 0.6,
        ("b1", "c0"): 0.7,
        ("b1", "c1"): 0.0,
    }

    def graded(a, b):
        return grades.get((a.body, b.body), 0.0)

    evaluator = AnyEquivalenceEvaluator(
        evaluator_identifier="GradedTrap_v1",
        evaluator=graded,
        ambiguity_threshold_fn=lambda _e: 0.4,
    )
    return AlignmentConfiguration(AlignmentProfile.strict_audit_v1, evaluator)


def _semantic_label_config() -> AlignmentConfiguration:
    def by_label(a, b):
        # Disagree with payload equality: score by semantic_label clusters.
        if getattr(a, "semantic_label", "") and a.semantic_label == getattr(
            b, "semantic_label", ""
        ):
            return 1.0
        return 0.0

    evaluator = AnyEquivalenceEvaluator(
        evaluator_identifier="SemanticLabel_v1",
        evaluator=by_label,
    )
    return AlignmentConfiguration(AlignmentProfile.strict_audit_v1, evaluator)


def _boundary_config(threshold: float) -> AlignmentConfiguration:
    """Profile with configurable semantic_threshold; payload evaluator returns fixed sims."""

    profile = AlignmentProfile(
        strategy=AlignmentStrategy.DEVELOPER_DEBUG,
        version=1,
        type_weight=0.0,
        payload_weight=1.0,
        structural_weight=0.0,
        temporal_weight=0.0,
        semantic_threshold=threshold,
        max_ambiguous_candidates=3,
        ambiguity_delta_threshold=0.10,
        alignment_mode=AlignmentMode.LINEAR,
    )

    # Map body strings to forced similarity values around the threshold.
    forced = {
        "sim-lo": 0.749999,
        "sim-eq": 0.75,
        "sim-hi": 0.750001,
    }

    def eval_sim(a, b):
        # Use comparison body as the forced similarity key when present.
        key = getattr(b, "body", "")
        if key in forced:
            return forced[key]
        key_a = getattr(a, "body", "")
        if key_a in forced:
            return forced[key_a]
        return 1.0 if a == b else 0.0

    evaluator = AnyEquivalenceEvaluator(
        evaluator_identifier="BoundaryProbe_v1",
        evaluator=eval_sim,
        ambiguity_threshold_fn=lambda _e: 0.4,
    )
    return AlignmentConfiguration(profile, evaluator)


def test_hungarian_maximizes_known_trap():
    # Classic: greedy would take (0,0)=5 leaving row1 empty of col0;
    # optimal takes (0,1)=4 + (1,0)=4 = 8.
    matrix = [
        [5.0, 4.0],
        [4.0, 0.0],
    ]
    pairs = hungarian_maximize(matrix)
    assert set(pairs) == {(0, 1), (1, 0)}
    assert sum(matrix[i][j] for i, j in pairs) == 8.0


def test_greedy_trap_disagreement_and_score():
    case = greedy_trap_assignment()
    config = _graded_trap_config()
    base = sorted(case.base.events, key=lambda e: e.sequence)
    comp = sorted(case.comparison.events, key=lambda e: e.sequence)
    greedy = greedy_bindings(config, base, comp)
    optimal = optimal_bindings(config, base, comp)
    assert total_score(optimal) + 1e-9 >= total_score(greedy)
    # With the graded landscape, pairings should differ.
    g_pairs = {(b.base_event_id, b.comparison_event_id) for b in greedy}
    o_pairs = {(b.base_event_id, b.comparison_event_id) for b in optimal}
    assert g_pairs != o_pairs
    metrics = evaluate_case(
        config, case.name, case.category, case.base, case.comparison, notes=case.notes
    )
    assert metrics.pairing_disagrees
    assert metrics.optimal_score_sum >= metrics.production_score_sum - 1e-9


def test_threshold_boundary_forced_scores():
    """0.749999 / 0.75 / 0.750001 against semantic_threshold=0.75."""
    from tests.adversarial_alignment.generators import AdvEvent, _mk_run

    config = _boundary_config(0.75)
    results = {}
    for label, sim_body in [
        ("below", "sim-lo"),
        ("at", "sim-eq"),
        ("above", "sim-hi"),
    ]:
        base = _mk_run([(0, AdvEvent("step", "anchor", critical=True))])
        comp = _mk_run([(0, AdvEvent("step", sim_body, critical=True))])
        result = TraceAlignmentEngine(config).align(base, comp)
        results[label] = result.regression_risk.level
        score, _ = config.score_match(base.events[0], comp.events[0])
        if label == "below":
            assert score < 0.75
            assert result.regression_risk.level is RegressionLevel.HIGH
        elif label == "at":
            assert abs(score - 0.75) < 1e-9
            # score >= threshold → not "changed beyond equivalence"
            assert result.regression_risk.level is RegressionLevel.NONE
        else:
            assert score > 0.75
            assert result.regression_risk.level is RegressionLevel.NONE
    print("threshold_boundary_results=", results)


def test_semantic_evaluator_disagree_hook():
    case = semantic_evaluator_disagree_hooks()
    config = _semantic_label_config()
    metrics = evaluate_case(
        config, case.name, case.category, case.base, case.comparison, notes=case.notes
    )
    # Labels cross-match → optimal/greedy should both bind cluster-aligned pairs;
    # ExactEquality would treat paraphrases as changed. This documents the hook works.
    assert metrics.production_pair_count == 2
    assert metrics.optimal_pair_count == 2
    assert not metrics.high_none_flip


@pytest.fixture(scope="module")
def exact_equality_suite_report(tmp_path_factory):
    config = _exact_equality_config()
    cases = [
        c
        for c in all_generator_cases()
        if c.name != "greedy_trap_assignment"  # needs graded evaluator
        and c.name != "semantic_evaluator_disagree"  # needs label evaluator
    ]
    # Add threshold boundary cases under developer_debug profile separately? Keep ExactEquality.
    report = run_suite(config, cases)

    # Also run graded trap + semantic hook as extra rows for the artifact.
    trap = greedy_trap_assignment()
    report.cases.append(
        evaluate_case(
            _graded_trap_config(),
            trap.name,
            trap.category,
            trap.base,
            trap.comparison,
            notes="graded",
        )
    )
    sem = semantic_evaluator_disagree_hooks()
    report.cases.append(
        evaluate_case(
            _semantic_label_config(),
            sem.name,
            sem.category,
            sem.base,
            sem.comparison,
            notes=sem.notes,
        )
    )

    out_dir = Path(tmp_path_factory.getbasetemp())
    artifact = out_dir / "adversarial_alignment_report.json"
    artifact.write_text(report.dumps(), encoding="utf-8")
    # Also write under workspace-friendly path when present.
    workspace_report = Path("/workspace/dpk-adversarial/adversarial_alignment_report.json")
    try:
        workspace_report.parent.mkdir(parents=True, exist_ok=True)
        workspace_report.write_text(report.dumps(), encoding="utf-8")
    except OSError:
        pass
    print("\n=== ADVERSARIAL ALIGNMENT METRICS ===")
    print(json.dumps(report.to_dict(), indent=2)[:4000])
    print(f"report_artifact={artifact}")
    return report


def test_suite_metrics_within_budget(exact_equality_suite_report):
    report = exact_equality_suite_report
    assert report.n >= 10
    assert report.disagreement_rate <= PAIRING_DISAGREEMENT_BUDGET
    assert report.verdict_flip_rate <= VERDICT_FLIP_BUDGET

    # ExactEquality_v1 cases only for the hard HIGH↔none invariant.
    exact_cases = [
        c
        for c in report.cases
        if c.name not in ("greedy_trap_assignment", "semantic_evaluator_disagree")
    ]
    unexpected = [c for c in exact_cases if c.high_none_flip]
    if unexpected:
        detail = json.dumps([c.__dict__ for c in unexpected], indent=2)
        pytest.fail(
            "Unexpected HIGH↔none verdict flips under ExactEquality_v1 "
            f"(budget={UNEXPECTED_HIGH_NONE_FLIP_BUDGET}):\n{detail}"
        )
    print(
        f"disagreement_rate={report.disagreement_rate:.3f} "
        f"verdict_flip_rate={report.verdict_flip_rate:.3f} "
        f"high_none_flip_rate={report.high_none_flip_rate:.3f}"
    )


def test_optimal_score_dominates_greedy(exact_equality_suite_report):
    for c in exact_equality_suite_report.cases:
        assert c.optimal_score_sum + 1e-9 >= c.production_score_sum, c.name


def test_categorized_failure_cases_printed(exact_equality_suite_report, capsys):
    cats = exact_equality_suite_report.categorized_failures()
    # Always print for CI logs even when empty.
    print("categorized_failures=", json.dumps(cats, indent=2))
    # Soft assertion: structure is a dict keyed by category.
    assert isinstance(cats, dict)

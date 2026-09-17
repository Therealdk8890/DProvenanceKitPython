"""Adversarial alignment suite: production greedy matcher vs optimal assignment.

Does NOT change the production TraceAlignmentEngine / DefaultTraceMatcher.
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
    SPECIAL_EVALUATOR_CASES,
    all_generator_cases,
    greedy_trap_assignment,
    greedy_trap_family,
    semantic_evaluator_disagree_hooks,
    semantic_hook_family,
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
MIN_SUITE_CASES = 40


def _exact_equality_config(profile: AlignmentProfile = None) -> AlignmentConfiguration:
    profile = profile or AlignmentProfile.strict_audit_v1
    evaluator = AnyEquivalenceEvaluator(
        evaluator_identifier="ExactEquality_v1",
        evaluator=lambda a, b: 1.0 if a == b else 0.0,
    )
    return AlignmentConfiguration(profile, evaluator)


def _graded_trap_config() -> AlignmentConfiguration:
    """Payload-graded evaluator that realizes classic greedy ≠ optimal traps."""

    # 2×2 classic:
    #   b0-c0: 0.8 → total 0.9; b0-c1: 0.6 → 0.8
    #   b1-c0: 0.7 → 0.85; b1-c1: 0.0 → 0.5
    # Optimal: (0,1)+(1,0)=1.65; greedy: (0,0)+(1,1)=1.4
    grades = {
        ("b0", "c0"): 0.8,
        ("b0", "c1"): 0.6,
        ("b1", "c0"): 0.7,
        ("b1", "c1"): 0.0,
        # 3×3: greedy prefers diagonal locals; optimal rotates.
        ("t3_b0", "t3_c0"): 0.9,
        ("t3_b0", "t3_c1"): 0.75,
        ("t3_b0", "t3_c2"): 0.1,
        ("t3_b1", "t3_c0"): 0.8,
        ("t3_b1", "t3_c1"): 0.2,
        ("t3_b1", "t3_c2"): 0.7,
        ("t3_b2", "t3_c0"): 0.15,
        ("t3_b2", "t3_c1"): 0.85,
        ("t3_b2", "t3_c2"): 0.55,
        # 4×4 sparse trap (high on a greedy-attracting column that starves others).
        ("t4_b0", "t4_c0"): 0.95,
        ("t4_b0", "t4_c1"): 0.7,
        ("t4_b1", "t4_c0"): 0.9,
        ("t4_b1", "t4_c2"): 0.75,
        ("t4_b2", "t4_c1"): 0.85,
        ("t4_b2", "t4_c3"): 0.65,
        ("t4_b3", "t4_c2"): 0.8,
        ("t4_b3", "t4_c3"): 0.2,
        # Steal-column: greedy takes (0,0)=0.92 leaving b1 with weak c1;
        # optimal (0,1)+(1,0) wins on total weight.
        ("steal_b0", "steal_c0"): 0.84,
        ("steal_b0", "steal_c1"): 0.7,
        ("steal_b1", "steal_c0"): 0.8,
        ("steal_b1", "steal_c1"): 0.05,
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

    forced = {
        "sim-lo": 0.749999,
        "sim-eq": 0.75,
        "sim-hi": 0.750001,
    }

    def eval_sim(a, b):
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


def _config_for_case(case) -> AlignmentConfiguration:
    if case.name.startswith("greedy_trap") or (case.notes or "").startswith("graded"):
        return _graded_trap_config()
    if case.category == "semantic_hook" or case.name.startswith("semantic_"):
        return _semantic_label_config()
    return _exact_equality_config()


def test_hungarian_maximizes_known_trap():
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
    g_pairs = {(b.base_event_id, b.comparison_event_id) for b in greedy}
    o_pairs = {(b.base_event_id, b.comparison_event_id) for b in optimal}
    assert g_pairs != o_pairs
    metrics = evaluate_case(
        config, case.name, case.category, case.base, case.comparison, notes=case.notes
    )
    assert metrics.pairing_disagrees
    assert metrics.optimal_score_sum >= metrics.production_score_sum - 1e-9


@pytest.mark.parametrize("trap", greedy_trap_family(), ids=lambda c: c.name)
def test_graded_trap_family_optimal_dominates(trap):
    config = _graded_trap_config()
    metrics = evaluate_case(
        config, trap.name, trap.category, trap.base, trap.comparison, notes=trap.notes
    )
    assert metrics.optimal_score_sum + 1e-9 >= metrics.production_score_sum


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
    assert metrics.production_pair_count == 2
    assert metrics.optimal_pair_count == 2
    assert not metrics.high_none_flip


@pytest.mark.parametrize("case", semantic_hook_family(), ids=lambda c: c.name)
def test_semantic_hook_family_binds(case):
    metrics = evaluate_case(
        _semantic_label_config(),
        case.name,
        case.category,
        case.base,
        case.comparison,
        notes=case.notes,
    )
    assert metrics.production_pair_count >= 1
    assert not metrics.high_none_flip


@pytest.fixture(scope="module")
def exact_equality_suite_report(tmp_path_factory):
    cases = all_generator_cases()
    report_cases = []
    for case in cases:
        config = _config_for_case(case)
        report_cases.append(
            evaluate_case(
                config,
                case.name,
                case.category,
                case.base,
                case.comparison,
                notes=getattr(case, "notes", "") or "",
            )
        )

    from tests.adversarial_alignment.compare import SuiteReport

    report = SuiteReport(cases=report_cases)

    out_dir = Path(tmp_path_factory.getbasetemp())
    artifact = out_dir / "adversarial_alignment_report.json"
    artifact.write_text(report.dumps(), encoding="utf-8")
    for path in (
        Path("/workspace/dpk-adversarial-v2/adversarial_alignment_report.json"),
        Path("/workspace/dpk-adversarial/adversarial_alignment_report.json"),
    ):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(report.dumps(), encoding="utf-8")
        except OSError:
            pass
    print("\n=== ADVERSARIAL ALIGNMENT METRICS (v2) ===")
    summary = {
        "n_cases": report.n,
        "disagreement_rate": report.disagreement_rate,
        "verdict_flip_rate": report.verdict_flip_rate,
        "high_none_flip_rate": report.high_none_flip_rate,
        "categorized_failures": report.categorized_failures(),
    }
    print(json.dumps(summary, indent=2))
    print(f"report_artifact={artifact}")
    return report


def test_suite_metrics_within_budget(exact_equality_suite_report):
    report = exact_equality_suite_report
    assert report.n >= MIN_SUITE_CASES, f"expected ≥{MIN_SUITE_CASES} cases, got {report.n}"
    assert report.disagreement_rate <= PAIRING_DISAGREEMENT_BUDGET
    assert report.verdict_flip_rate <= VERDICT_FLIP_BUDGET

    exact_cases = [
        c for c in report.cases if c.name not in SPECIAL_EVALUATOR_CASES
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
        f"high_none_flip_rate={report.high_none_flip_rate:.3f} "
        f"n={report.n}"
    )


def test_optimal_score_dominates_greedy(exact_equality_suite_report):
    for c in exact_equality_suite_report.cases:
        assert c.optimal_score_sum + 1e-9 >= c.production_score_sum, c.name


def test_categorized_failure_cases_printed(exact_equality_suite_report, capsys):
    cats = exact_equality_suite_report.categorized_failures()
    print("categorized_failures=", json.dumps(cats, indent=2))
    assert isinstance(cats, dict)


def test_generator_catalog_has_long_and_fuzz_coverage():
    cases = all_generator_cases()
    names = {c.name for c in cases}
    cats = {c.category for c in cases}
    assert any(n.startswith("long_repeated_") for n in names)
    assert any(n.startswith("fuzz_seed_") for n in names)
    assert "ties" in cats and "collisions" in cats and "fuzz" in cats
    assert len(cases) >= MIN_SUITE_CASES

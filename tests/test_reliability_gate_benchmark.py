"""Regression tests for the independent-label gate policy sensitivity report."""

from __future__ import annotations

from examples.reliability_gate_benchmark import SCENARIOS, run_benchmark


def test_each_case_has_a_unique_label_and_both_ground_truth_classes_are_present():
    names = [case.name for case in SCENARIOS]
    assert len(names) == len(set(names))
    assert any(case.expected_regression for case in SCENARIOS)
    assert any(not case.expected_regression for case in SCENARIOS)


def test_policy_sensitivity_report_is_repeatable_and_counts_every_case():
    report = run_benchmark()
    assert report == run_benchmark()
    assert report["scenario_count"] == len(SCENARIOS)
    for policy in report["policies"].values():
        confusion = policy["confusion_matrix"]
        assert sum(confusion.values()) == len(SCENARIOS)
        assert confusion["tp"] + confusion["fn"] == sum(c.expected_regression for c in SCENARIOS)
        assert confusion["fp"] + confusion["tn"] == sum(not c.expected_regression for c in SCENARIOS)
        assert len(policy["cases"]) == len(SCENARIOS)


def test_both_policies_keep_high_priority_regressions_blocked():
    report = run_benchmark()
    for policy in report["policies"].values():
        assert policy["missed_regression_cases"] == []
        assert policy["confusion_matrix"]["fn"] == 0


def test_severity_only_policy_tolerates_noncritical_payload_changes():
    report = run_benchmark()
    strict = report["policies"]["strict_default"]
    lenient = report["policies"]["severity_only"]
    assert "benign_retrieval_payload" in strict["false_positive_cases"]
    assert "benign_retrieval_payload" not in lenient["false_positive_cases"]
    assert lenient["confusion_matrix"]["fp"] < strict["confusion_matrix"]["fp"]

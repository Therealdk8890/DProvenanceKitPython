"""Contract-level checks for experimental Structural Alignment v1 fixtures.

These assertions validate the language-neutral contract vectors only. They do not
claim that the production alignment engine implements Structural Alignment v1.
"""

from __future__ import annotations

import json
from pathlib import Path

VECTORS = (
    Path(__file__).resolve().parents[1]
    / "conformance"
    / "vectors"
    / "structural_alignment_v1.json"
)


def _load_cases() -> list[dict]:
    return json.loads(VECTORS.read_text(encoding="utf-8"))["cases"]


def test_structural_alignment_expected_metrics_are_internally_consistent() -> None:
    for case in _load_cases():
        expected = case["expected"]
        tp, fp, fn = expected["tp"], expected["fp"], expected["fn"]
        denominator = 2 * tp + fp + fn
        computed_f1 = (2 * tp / denominator) if denominator else 0.0
        assert computed_f1 == expected["global_f1"], case["id"]


def test_inclusive_threshold_mapping_does_not_override_hierarchy_gate() -> None:
    case = next(
        item for item in _load_cases()
        if item["id"] == "inclusive_threshold_boundary"
    )
    expected = case["expected"]
    child = next(
        verdict for verdict in expected["verdicts"]
        if verdict["target_id"] == "child"
    )

    assert case["configuration"]["min_node_conformance"] == 0.7
    assert child["node_score"] == 0.7
    assert child["matched"] is True
    assert child["gate_passed"] is False
    assert child["reason"] == "PARENT_HIERARCHY_MISMATCH"

    # The eligible pair contributes to F1, but the independent hierarchy gate fails.
    assert (expected["tp"], expected["fp"], expected["fn"]) == (2, 0, 0)
    assert expected["global_f1"] == 1.0
    assert expected["all_required_nodes_meet_score_threshold"] is True
    assert expected["order_gate_passed"] is True
    assert expected["hierarchy_gate_passed"] is False
    assert expected["conforming"] is False

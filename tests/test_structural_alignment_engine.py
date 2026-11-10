"""Production-engine tests for experimental Structural Alignment v1."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dprovenancekit.structural_alignment import (
    StructuralAlignmentConfig,
    StructuralAlignmentEngineV1,
    StructuralAlignmentInputError,
)

VECTORS = (
    Path(__file__).resolve().parents[1]
    / "conformance"
    / "vectors"
    / "structural_alignment_v1.json"
)


def _document() -> dict:
    return json.loads(VECTORS.read_text(encoding="utf-8"))


def _engine_for(case: dict) -> StructuralAlignmentEngineV1:
    config = case.get("configuration", {})
    weights = config.get("weights", _document()["default_configuration"]["weights"])
    defaults = _document()["default_configuration"]
    return StructuralAlignmentEngineV1(
        StructuralAlignmentConfig(
            type_weight=weights["type"] / 10000,
            hierarchy_weight=weights["hierarchy"] / 10000,
            payload_weight=weights["payload"] / 10000,
            min_node_conformance=config.get(
                "min_node_conformance", defaults["min_node_conformance"]
            ) / 10000,
            min_global_f1_floor=config.get(
                "min_global_f1_floor", defaults["min_global_f1_floor"]
            ) / 10000,
        )
    )


@pytest.mark.parametrize("case", _document()["cases"], ids=lambda case: case["id"])
def test_engine_reproduces_every_contract_fixture(case: dict) -> None:
    result = _engine_for(case).align(case["target"], case["candidate"])
    expected = case["expected"]

    assert (result.tp, result.fp, result.fn) == (
        expected["tp"], expected["fp"], expected["fn"]
    )
    assert result.global_f1 == pytest.approx(expected["global_f1"])
    assert result.conforming is expected["conforming"]
    if "f1_gate_passed" in expected:
        assert result.f1_gate_passed is expected["f1_gate_passed"]
    if "empty_input" in expected:
        assert result.empty_input is expected["empty_input"]
    if "hierarchy_gate_passed" in expected:
        assert result.hierarchy_gate_passed is expected["hierarchy_gate_passed"]
    if "order_gate_passed" in expected:
        assert result.order_gate_passed is expected["order_gate_passed"]
    if "all_required_nodes_meet_score_threshold" in expected:
        assert (
            result.all_required_nodes_meet_score_threshold
            is expected["all_required_nodes_meet_score_threshold"]
        )

    actual = {verdict.target_id: verdict for verdict in result.verdicts}
    for expected_verdict in expected["verdicts"]:
        verdict = actual[expected_verdict["target_id"]]
        for field in (
            "candidate_id",
            "suggested_candidate_id",
            "matched",
            "gate_passed",
            "reason",
        ):
            if field in expected_verdict:
                assert getattr(verdict, field) == expected_verdict[field]
        for field in (
            "type_score",
            "hierarchy_score",
            "payload_score",
            "node_score",
        ):
            if field in expected_verdict:
                assert getattr(verdict, field) == pytest.approx(
                    expected_verdict[field]
                )


def test_zero_score_candidate_is_not_emitted_as_diagnostic_suggestion() -> None:
    case = next(
        item for item in _document()["cases"]
        if item["id"] == "missing_and_hierarchy_drift"
    )
    result = _engine_for(case).align(case["target"], case["candidate"])
    missing = next(v for v in result.verdicts if v.target_id == "c")
    assert missing.suggested_candidate_id is None
    assert missing.reason == "MISSING_REQUIRED_EVENT"


@pytest.mark.parametrize(
    "target,candidate",
    [
        (
            [{"id": "x", "parent_id": "missing", "type": "A", "payload_signature": "a"}],
            [],
        ),
        (
            [
                {"id": "x", "parent_id": "y", "type": "A", "payload_signature": "a"},
                {"id": "y", "parent_id": "x", "type": "B", "payload_signature": "b"},
            ],
            [],
        ),
        (
            [
                {"id": "x", "parent_id": None, "type": "A", "payload_signature": "a"},
                {"id": "x", "parent_id": None, "type": "B", "payload_signature": "b"},
            ],
            [],
        ),
    ],
)
def test_invalid_target_graph_is_rejected(target: list[dict], candidate: list[dict]) -> None:
    with pytest.raises(StructuralAlignmentInputError):
        StructuralAlignmentEngineV1().align(target, candidate)


def test_invalid_configuration_is_rejected() -> None:
    with pytest.raises(StructuralAlignmentInputError):
        StructuralAlignmentConfig(type_weight=0.5, hierarchy_weight=0.2, payload_weight=0.2)


def test_engine_is_separate_from_legacy_alignment_engine() -> None:
    from dprovenancekit.alignment_engine import TraceAlignmentEngine

    assert StructuralAlignmentEngineV1 is not TraceAlignmentEngine


def test_parent_matching_is_topological_even_when_parent_follows_child_in_input() -> None:
    target = [
        {"id": "child", "parent_id": "root", "type": "CHILD", "payload_signature": "c"},
        {"id": "root", "parent_id": None, "type": "ROOT", "payload_signature": "r"},
    ]
    candidate = [
        {"id": "child-c", "parent_id": "root-c", "type": "CHILD", "payload_signature": "c"},
        {"id": "root-c", "parent_id": None, "type": "ROOT", "payload_signature": "r"},
    ]
    result = StructuralAlignmentEngineV1().align(target, candidate)
    actual = {v.target_id: v for v in result.verdicts}
    assert actual["root"].candidate_id == "root-c"
    assert actual["child"].candidate_id == "child-c"
    assert actual["child"].hierarchy_score == 1.0


def test_equal_score_assignment_uses_candidate_sequence_then_target_sequence() -> None:
    target = [
        {"id": "target-1", "parent_id": None, "type": "STEP", "payload_signature": "same"},
        {"id": "target-2", "parent_id": None, "type": "STEP", "payload_signature": "same"},
    ]
    candidate = [
        {"id": "candidate-1", "parent_id": None, "type": "STEP", "payload_signature": "same"},
        {"id": "candidate-2", "parent_id": None, "type": "STEP", "payload_signature": "same"},
    ]
    result = StructuralAlignmentEngineV1().align(target, candidate)
    actual = {v.target_id: v.candidate_id for v in result.verdicts}
    assert actual == {"target-1": "candidate-1", "target-2": "candidate-2"}

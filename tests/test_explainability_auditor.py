"""Ports ExplainabilityAuditorTests."""

from __future__ import annotations

from dprovenancekit import (
    BindingDecision,
    EquivalenceDecisionRecord,
    EquivalenceReason,
    ExplainabilityAuditor,
    FormalizationMap,
    InterpretationStep,
)

auditor = ExplainabilityAuditor()


def _step(base, comp, state, bseq=None, cseq=None):
    return InterpretationStep(
        source_binding=None,
        base_id=base,
        comparison_id=comp,
        output_state=state,
        rationale="",
        base_sequence=bseq,
        comparison_sequence=cseq,
    )


def _binding(base, comp):
    return BindingDecision(base_id=base, comparison_id=comp, similarity_score=1.0)


def _decision(base, comp, equivalent):
    return EquivalenceDecisionRecord(
        lhs=base, rhs=comp, confidence=0.95 if equivalent else 0.2,
        equivalent=equivalent, reason=EquivalenceReason(""),
    )


def test_empty_map_is_perfect():
    v = auditor.audit(FormalizationMap(bindings=[], decisions=[], interpretations=[]))
    assert (v.coverage, v.completeness, v.causal_ordering, v.no_hallucinations) == (1.0, 1.0, 1.0, 1.0)


def test_fully_grounded_match_is_perfect():
    m = FormalizationMap(
        bindings=[_binding("b", "c")],
        decisions=[_decision("b", "c", True)],
        interpretations=[_step("b", "c", "semanticMatch(strength: 0.9)", 0, 0)],
    )
    v = auditor.audit(m)
    assert (v.coverage, v.completeness, v.causal_ordering, v.no_hallucinations) == (1.0, 1.0, 1.0, 1.0)


def test_ungrounded_match_hurts_coverage_only():
    m = FormalizationMap(
        bindings=[],
        decisions=[_decision("b", "c", True)],
        interpretations=[_step("b", "c", "semanticMatch(strength: 0.9)", 0, 0)],
    )
    v = auditor.audit(m)
    assert v.coverage == 0.0
    assert v.completeness == 1.0
    assert v.no_hallucinations == 1.0


def test_unevaluated_match_hurts_completeness():
    m = FormalizationMap(
        bindings=[_binding("b", "c")],
        decisions=[],
        interpretations=[_step("b", "c", "semanticMatch(strength: 0.9)", 0, 0)],
    )
    v = auditor.audit(m)
    assert v.coverage == 1.0
    assert v.completeness == 0.0


def test_unsupported_claim_is_hallucination():
    m = FormalizationMap(
        bindings=[_binding("b", "c")],
        decisions=[_decision("b", "c", False)],
        interpretations=[_step("b", "c", "semanticMatch(strength: 0.5)", 0, 0)],
    )
    v = auditor.audit(m)
    assert v.coverage == 1.0
    assert v.completeness == 1.0
    assert v.no_hallucinations == 0.0


def test_ambiguous_verdict_is_not_hallucination():
    m = FormalizationMap(
        bindings=[_binding("b", "c")],
        decisions=[_decision("b", "c", False)],
        interpretations=[_step("b", "c", "ambiguous(optionsCount: 2)", 0, 0)],
    )
    assert auditor.audit(m).no_hallucinations == 1.0


def test_order_preserving_alignment_is_faithful():
    m = FormalizationMap(
        bindings=[_binding("a", "x"), _binding("b", "y")],
        decisions=[_decision("a", "x", True), _decision("b", "y", True)],
        interpretations=[
            _step("a", "x", "semanticMatch(strength: 0.9)", 0, 0),
            _step("b", "y", "semanticMatch(strength: 0.9)", 1, 1),
        ],
    )
    assert auditor.audit(m).causal_ordering == 1.0


def test_unreported_reorder_hurts_ordering():
    m = FormalizationMap(
        bindings=[_binding("a", "y"), _binding("b", "x")],
        decisions=[_decision("a", "y", True), _decision("b", "x", True)],
        interpretations=[
            _step("a", "y", "semanticMatch(strength: 0.9)", 0, 1),
            _step("b", "x", "semanticMatch(strength: 0.9)", 1, 0),
        ],
    )
    assert auditor.audit(m).causal_ordering == 0.0


def test_reported_reorder_is_faithful():
    m = FormalizationMap(
        bindings=[_binding("a", "y"), _binding("b", "x")],
        decisions=[_decision("a", "y", True), _decision("b", "x", True)],
        interpretations=[
            _step("a", "y", "reordered(originalSequence: 0, newSequence: 1)", 0, 1),
            _step("b", "x", "reordered(originalSequence: 1, newSequence: 0)", 1, 0),
        ],
    )
    assert auditor.audit(m).causal_ordering == 1.0

# git-blob-rewrite

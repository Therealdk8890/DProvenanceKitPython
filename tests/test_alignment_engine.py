"""Ports TraceAlignmentEngineTests."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest

from dprovenancekit import (
    AlignmentConfiguration,
    AlignmentMode,
    AlignmentProfile,
    AlignmentSnapshot,
    AlignmentSnapshotValidator,
    AlignmentStateKind,
    AlignmentStrategy,
    AnyEquivalenceEvaluator,
    DefaultFormalizationMapBuilder,
    DriftToleranceMode,
    ExplainabilityAuditor,
    SnapshotValidationError,
    TraceAlignmentEngine,
    TraceEvent,
    TracePriority,
    TraceableEvent,
    TraceRun,
    VerificationCaptureMode,
)


@dataclass(frozen=True)
class AlignEvent(TraceableEvent):
    name: str

    @property
    def type_identifier(self) -> str:
        return self.name

    @property
    def priority(self) -> TracePriority:
        return TracePriority.STRUCTURAL


def _run(run_id, seq_events):
    events = [
        TraceEvent(
            run_id=run_id,
            context_id="test_ctx",
            engine_name=engine,
            schema_version=1,
            sequence=seq,
            span_id=None,
            parent_span_id=None,
            payload=payload,
        )
        for (seq, engine, payload) in seq_events
    ]
    return TraceRun(run_id=run_id, context_id="test_ctx", events=events)


def test_level1_structural_correctness():
    run_a, run_b = uuid.uuid4(), uuid.uuid4()
    base = _run(run_a, [
        (0, "engine1", AlignEvent("stepA")),
        (1, "engine1", AlignEvent("stepB")),
        (2, "engine1", AlignEvent("stepC")),
    ])
    comp = _run(run_b, [
        (0, "engine1", AlignEvent("stepA")),
        (1, "engine1", AlignEvent("stepC")),
        (2, "engine1", AlignEvent("stepD")),
    ])
    evaluator = AnyEquivalenceEvaluator(
        evaluator_identifier="exact",
        evaluator=lambda a, b: 1.0 if a.type_identifier == b.type_identifier else 0.0,
    )
    config = AlignmentConfiguration(AlignmentProfile.strict_audit_v1, evaluator)
    result = TraceAlignmentEngine(config).align(base, comp)

    assert len(result.alignments) == 4
    removed = next(a for a in result.alignments if a.state.is_removed)
    assert removed.base_event.payload.type_identifier == "stepB"
    added = next(a for a in result.alignments if a.state.kind == AlignmentStateKind.ADDED)
    assert added.comparison_event.payload.type_identifier == "stepD"
    assert result.engine_version == "v2-causal-strict"
    assert result.profile_hash


def test_ambiguity_bounding():
    run_a, run_b = uuid.uuid4(), uuid.uuid4()
    base = _run(run_a, [(0, "engine1", AlignEvent("query"))])
    comp = _run(run_b, [
        (0, "engine1", AlignEvent("fetch1")),
        (1, "engine1", AlignEvent("fetch2")),
        (2, "engine1", AlignEvent("fetch3")),
        (3, "engine1", AlignEvent("fetch4")),
    ])
    evaluator = AnyEquivalenceEvaluator(
        evaluator_identifier="mock_ambiguity",
        evaluator=lambda a, b: 0.85,
        ambiguity_threshold_fn=lambda _e: 0.80,
    )
    profile = AlignmentProfile(
        strategy=AlignmentStrategy.SEMANTIC_EXPLORATION,
        version=1,
        type_weight=0.0,
        payload_weight=1.0,
        structural_weight=0.0,
        temporal_weight=0.0,
        semantic_threshold=0.99,
        max_ambiguous_candidates=2,
        ambiguity_delta_threshold=0.05,
        alignment_mode=AlignmentMode.LINEAR,
    )
    config = AlignmentConfiguration(profile, evaluator)
    result = TraceAlignmentEngine(config).align(base, comp)

    ambiguous = [a for a in result.alignments if a.state.kind == AlignmentStateKind.AMBIGUOUS]
    assert len(ambiguous) == 1
    assert len(ambiguous[0].ambiguous_candidates) == 2


def test_snapshot_drift_validation():
    run_a, run_b = uuid.uuid4(), uuid.uuid4()
    base = _run(run_a, [(0, "engine1", AlignEvent("stepA"))])
    comp = _run(run_b, [(0, "engine1", AlignEvent("stepB"))])
    evaluator = AnyEquivalenceEvaluator(
        evaluator_identifier="exact",
        evaluator=lambda a, b: 1.0 if a.type_identifier == b.type_identifier else 0.0,
    )
    config = AlignmentConfiguration(AlignmentProfile.strict_audit_v1, evaluator)
    result = TraceAlignmentEngine(config).align(base, comp)

    snapshot = AlignmentSnapshotValidator.create_snapshot(result)
    validator = AlignmentSnapshotValidator(DriftToleranceMode.STRICT)
    assert validator.validate(result, snapshot)

    bad = AlignmentSnapshot(snapshot.profile_hash, snapshot.engine_version, "bad_hash")
    with pytest.raises(SnapshotValidationError):
        validator.validate(result, bad)

    report_validator = AlignmentSnapshotValidator(DriftToleranceMode.REPORT_ONLY)
    assert report_validator.validate(result, bad) is False


def test_formalization_map_produces_correct_causal_graph():
    run_a, run_b = uuid.uuid4(), uuid.uuid4()
    base = _run(run_a, [
        (0, "engine1", AlignEvent("stepA")),
        (1, "engine1", AlignEvent("stepB")),
    ])
    comp = _run(run_b, [
        (0, "engine1", AlignEvent("stepA")),
        (1, "engine1", AlignEvent("stepB")),
    ])
    evaluator = AnyEquivalenceEvaluator(
        evaluator_identifier="exact",
        evaluator=lambda a, b: 1.0 if a.type_identifier == b.type_identifier else 0.0,
    )
    config = AlignmentConfiguration(AlignmentProfile.strict_audit_v1, evaluator)
    engine = TraceAlignmentEngine(config, capture_mode=VerificationCaptureMode.EVIDENCE_ONLY)
    result = engine.align(base, comp)

    assert result.verification_artifacts is not None
    evidence = result.verification_artifacts.evidence
    m = DefaultFormalizationMapBuilder().build(evidence)
    assert len(m.bindings) == 2
    assert len(m.decisions) == 3
    assert len(m.interpretations) == 2

    vector = ExplainabilityAuditor().audit(m)
    assert vector.coverage == 1.0
    assert vector.completeness == 1.0
    assert vector.causal_ordering == 1.0
    assert vector.no_hallucinations == 1.0

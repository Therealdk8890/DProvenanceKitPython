"""Ports StabilityEvaluationTests."""

from __future__ import annotations

import uuid

from dprovenancekit import (
    AlignmentConfiguration,
    AlignmentFinding,
    AlignmentProfile,
    AnyEquivalenceEvaluator,
    BenchmarkCase,
    BenchmarkDataset,
    BenchmarkRunner,
    DeterministicBoundary,
    DProvenanceCorpus,
    EvaluationPerturbationLayer,
    ExpectedFinding,
    TraceAlignmentEngine,
    TraceEvent,
    TraceRun,
    VerificationCaptureMode,
)
from dprovenancekit.perturbation import PerturbationMode

AgentEvent = DProvenanceCorpus.AgentEvent


def _tool(name, seq):
    return TraceEvent(
        run_id=uuid.uuid4(),
        context_id="t",
        engine_name="Agent",
        schema_version=1,
        sequence=seq,
        span_id="s",
        parent_span_id=None,
        payload=AgentEvent.tool_execution(name, ""),
    )


def _single_case_dataset():
    case = BenchmarkCase(
        name="tool-substitution",
        description="",
        base_run=TraceRun(
            run_id=uuid.uuid4(), context_id="t", events=[_tool("Search", 0)]
        ),
        comparison_run=TraceRun(
            run_id=uuid.uuid4(), context_id="t", events=[_tool("Lookup", 0)]
        ),
        expected_findings=[
            ExpectedFinding(AlignmentFinding.semantic_evolution("tool", "tool"))
        ],
    )
    return BenchmarkDataset(name="stability", description="", cases=[case])


def test_deterministic_engine_has_zero_variance():
    runner = BenchmarkRunner()
    boundary = DeterministicBoundary(cache_isolated=True, seed_control="fixed")

    def factory(_ctx, cb):
        evaluator = AnyEquivalenceEvaluator(
            evaluator_identifier="det",
            evaluator=lambda b, c: (
                0.95
                if (b.type_identifier == "tool" and c.type_identifier == "tool")
                else 0.0
            ),
        )
        config = AlignmentConfiguration(AlignmentProfile.developer_debug_v1, evaluator)
        return TraceAlignmentEngine(
            config,
            capture_mode=VerificationCaptureMode.EVIDENCE_ONLY,
            meta_trace_callback=cb,
        )

    stability = runner.run_repeated_evaluation(
        _single_case_dataset(), iterations=3, engine_factory=factory, boundary=boundary
    )
    assert abs(stability.f1_variance) < 1e-12
    assert stability.drift_fingerprint == "Stable: No significant drift"


def test_non_deterministic_engine_produces_detectable_variance():
    runner = BenchmarkRunner()
    boundary = DeterministicBoundary(cache_isolated=False, seed_control=None)

    def factory(ctx, cb):
        match_score = 0.95 if ctx.iteration % 2 == 0 else 0.30
        evaluator = AnyEquivalenceEvaluator(
            evaluator_identifier="drifty",
            evaluator=lambda b, c, ms=match_score: (
                ms
                if (b.type_identifier == "tool" and c.type_identifier == "tool")
                else 0.0
            ),
        )
        config = AlignmentConfiguration(AlignmentProfile.developer_debug_v1, evaluator)
        return TraceAlignmentEngine(
            config,
            capture_mode=VerificationCaptureMode.EVIDENCE_ONLY,
            meta_trace_callback=cb,
        )

    stability = runner.run_repeated_evaluation(
        _single_case_dataset(), iterations=3, engine_factory=factory, boundary=boundary
    )
    assert stability.f1_variance > 0.0
    assert stability.drift_fingerprint != "Stable: No significant drift"


def test_perturbation_is_gated_by_boundary():
    base = AnyEquivalenceEvaluator(
        evaluator_identifier="base", evaluator=lambda a, b: 0.9
    )
    layer = EvaluationPerturbationLayer(PerturbationMode.score_noise(0.2))

    isolated = layer.evaluator(base, DeterministicBoundary(cache_isolated=True))
    assert isolated.evaluator_identifier == "base"

    leaky = layer.evaluator(base, DeterministicBoundary(cache_isolated=False))
    assert leaky.evaluator_identifier == "base+noise"

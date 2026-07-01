"""Ports BenchmarkRunnerTests."""

from __future__ import annotations

import uuid

from dprovenancekit import (
    AlignmentConfiguration,
    AlignmentFinding,
    AlignmentFindingKind,
    AlignmentProfile,
    BenchmarkCase,
    BenchmarkDataset,
    BenchmarkRunner,
    DProvenanceCorpus,
    ExpectedFinding,
    TraceAlignmentEngine,
    TraceEvent,
    TraceRun,
    VerificationCaptureMode,
)

AgentEvent = DProvenanceCorpus.AgentEvent


def _factory(capture=VerificationCaptureMode.EVIDENCE_ONLY):
    def make(callback):
        config = AlignmentConfiguration(
            AlignmentProfile.developer_debug_v1, DProvenanceCorpus.standard_evaluator()
        )
        return TraceAlignmentEngine(
            config, capture_mode=capture, meta_trace_callback=callback
        )

    return make


def _ev(payload, seq):
    return TraceEvent(
        run_id=uuid.uuid4(),
        context_id="t",
        engine_name="Agent",
        schema_version=1,
        sequence=seq,
        span_id="s",
        parent_span_id=None,
        payload=payload,
    )


def _run(events, comp, expect, capture=VerificationCaptureMode.EVIDENCE_ONLY):
    case = BenchmarkCase(
        name="adhoc",
        description="",
        base_run=TraceRun(run_id=uuid.uuid4(), context_id="t", events=events),
        comparison_run=TraceRun(run_id=uuid.uuid4(), context_id="t", events=comp),
        expected_findings=expect,
    )
    dataset = BenchmarkDataset(name="t", description="", cases=[case])
    report = BenchmarkRunner().run(dataset, _factory(capture))
    return report.case_results[0]


def _decision(action, seq):
    return _ev(AgentEvent.decision(action), seq)


def _tool(name, seq):
    return _ev(AgentEvent.tool_execution(name, ""), seq)


def test_standard_corpus_is_scored_and_non_degenerate():
    report = BenchmarkRunner().run(DProvenanceCorpus.dataset(), _factory())
    assert report.total_cases == 8
    assert report.global_metrics.true_positives > 0
    assert report.passed_cases == report.total_cases
    assert abs(report.global_metrics.precision - 1.0) < 1e-9
    assert abs(report.global_metrics.recall - 1.0) < 1e-9
    assert report.average_fidelity_score > 0.0


def test_diagnoser_classifies_reorder_false_positives():
    base, comp = DProvenanceCorpus.reordering()
    case = BenchmarkCase(
        name="reorder-fp",
        description="",
        base_run=base,
        comparison_run=comp,
        expected_findings=[],
    )
    report = BenchmarkRunner().run(
        BenchmarkDataset(name="t", description="", cases=[case]), _factory()
    )
    result = report.case_results[0]
    assert result.false_positives
    reorder_diag = next(
        (
            d
            for d in result.diagnoses
            if d.finding.kind == AlignmentFindingKind.REORDERED_EXECUTION
        ),
        None,
    )
    assert reorder_diag is not None
    from dprovenancekit import FailureCause

    assert reorder_diag.hypothesized_cause != FailureCause.undiagnosed()


def test_well_defined_cases_pass():
    report = BenchmarkRunner().run(DProvenanceCorpus.dataset(), _factory())
    by_name = {c.benchmark_case.name: c for c in report.case_results}
    assert by_name["Semantic Evolution"].passed
    assert by_name["Semantic Drift"].passed
    assert by_name["Degenerate Traces"].passed


def test_fidelity_requires_evidence_capture():
    base = [_tool("Search", 0)]
    comp = [_tool("Lookup", 0)]
    evid = _run(base, comp, [], capture=VerificationCaptureMode.EVIDENCE_ONLY)
    no_evid = _run(base, comp, [], capture=VerificationCaptureMode.DISABLED)
    assert evid.actual_findings
    assert evid.fidelity_score.overall_score > 0.0
    assert no_evid.fidelity_score.overall_score == 0.0


def test_multiset_matching_consumes_duplicates():
    base = [_decision("ValidateA", 0), _decision("ValidateB", 1)]
    comp = []

    two = _run(
        base,
        comp,
        [
            ExpectedFinding(AlignmentFinding.critical_step_removed("decision")),
            ExpectedFinding(AlignmentFinding.critical_step_removed("decision")),
        ],
    )
    assert (
        sum(
            1
            for f in two.true_positives
            if f.kind == AlignmentFindingKind.CRITICAL_STEP_REMOVED
        )
        == 2
    )

    one = _run(
        base,
        comp,
        [ExpectedFinding(AlignmentFinding.critical_step_removed("decision"))],
    )
    assert (
        sum(
            1
            for f in one.true_positives
            if f.kind == AlignmentFindingKind.CRITICAL_STEP_REMOVED
        )
        == 1
    )
    assert any(
        f.kind == AlignmentFindingKind.CRITICAL_STEP_REMOVED
        for f in one.false_positives
    )


def test_empty_findings_with_missed_expectation_scores_zero():
    result = _run(
        [], [], [ExpectedFinding(AlignmentFinding.critical_step_removed("decision"))]
    )
    assert not result.actual_findings
    assert result.false_negatives
    assert result.fidelity_score.overall_score == 0.0

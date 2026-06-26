"""Headless evaluator CLI: runs the standard corpus through the real benchmark runner.

Mirrors the Swift ``DProvenanceKitCLI``. Usage::

    dprovenancekit <evaluate|diagnose|stability>
"""

from __future__ import annotations

import sys

from .alignment_config import (
    AlignmentConfiguration,
    AlignmentMode,
    AlignmentProfile,
    AlignmentStrategy,
    AnyEquivalenceEvaluator,
)
from .alignment_engine import TraceAlignmentEngine, VerificationCaptureMode
from .benchmark import BenchmarkRunner, DeterministicBoundary
from .corpus import DProvenanceCorpus


def _make_engine(callback) -> TraceAlignmentEngine:
    config = AlignmentConfiguration(
        profile=AlignmentProfile.developer_debug_v1,
        equivalence_evaluator=DProvenanceCorpus.standard_evaluator(),
    )
    return TraceAlignmentEngine(config, capture_mode=VerificationCaptureMode.EVIDENCE_ONLY, meta_trace_callback=callback)


def _print_case_line(c) -> None:
    print(
        "  [{}] {}  TP={} FP={} FN={}  fidelity={:.2f}".format(
            "PASS" if c.passed else "FAIL",
            c.benchmark_case.name,
            len(c.true_positives),
            len(c.false_positives),
            len(c.false_negatives),
            c.fidelity_score.overall_score,
        )
    )


def main(argv=None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    print("DProvenanceKit CLI Evaluator")
    print("============================")

    mode = argv[0] if argv else "evaluate"
    if mode not in ("evaluate", "diagnose", "stability"):
        print("Usage: dprovenancekit <evaluate|diagnose|stability>")
        return 0

    runner = BenchmarkRunner()
    dataset = DProvenanceCorpus.dataset()

    if mode == "evaluate":
        print("=== STANDARD DATASET ===")
        report = runner.run(dataset, lambda cb: _make_engine(cb))
        print(
            "Dataset: {}  ({} cases, {} passed)".format(
                report.dataset_name, report.total_cases, report.passed_cases
            )
        )
        print(
            "Precision: {:.3f}  Recall: {:.3f}  F1: {:.3f}".format(
                report.global_metrics.precision,
                report.global_metrics.recall,
                report.global_metrics.f1_score,
            )
        )
        print(
            "Avg fidelity: {:.3f}  Avg runtime: {:.2f}ms  p95: {:.2f}ms".format(
                report.average_fidelity_score, report.average_run_time_ms, report.p95_run_time_ms
            )
        )
        for c in report.case_results:
            _print_case_line(c)

        print("\n=== ADVERSARIAL DATASET ===")
        adv_dataset = DProvenanceCorpus.adversarial_dataset()

        def adv_engine(cb):
            adv_profile = AlignmentProfile(
                strategy=AlignmentStrategy.DEVELOPER_DEBUG,
                version=2,
                type_weight=0.4,
                payload_weight=0.4,
                structural_weight=0.15,
                temporal_weight=0.05,
                semantic_threshold=0.85,
                max_ambiguous_candidates=1,
                ambiguity_delta_threshold=0.15,
                alignment_mode=AlignmentMode.SPAN_AWARE,
            )
            config = AlignmentConfiguration(
                profile=adv_profile, equivalence_evaluator=DProvenanceCorpus.standard_evaluator()
            )
            return TraceAlignmentEngine(
                config, capture_mode=VerificationCaptureMode.EVIDENCE_ONLY, meta_trace_callback=cb
            )

        adv_report = runner.run(adv_dataset, adv_engine)
        print(
            "Dataset: {}  ({} cases, {} passed)".format(
                adv_report.dataset_name, adv_report.total_cases, adv_report.passed_cases
            )
        )
        print(
            "Precision: {:.3f}  Recall: {:.3f}  F1: {:.3f}".format(
                adv_report.global_metrics.precision,
                adv_report.global_metrics.recall,
                adv_report.global_metrics.f1_score,
            )
        )
        print(
            "Avg fidelity: {:.3f}  Avg runtime: {:.2f}ms  p95: {:.2f}ms".format(
                adv_report.average_fidelity_score,
                adv_report.average_run_time_ms,
                adv_report.p95_run_time_ms,
            )
        )
        for c in adv_report.case_results:
            _print_case_line(c)

        print("\n=== SUMMARY ===")
        total_cases = report.total_cases + adv_report.total_cases
        total_passed = report.passed_cases + adv_report.passed_cases
        print(f"Total Cases: {total_cases}")
        print("Total Passed: {} ({:.1f}%)".format(total_passed, total_passed / total_cases * 100))

    elif mode == "diagnose":
        report = runner.run(dataset, lambda cb: _make_engine(cb))
        print("Causal ranking (most systemically impactful failure modes first):")
        ranking = report.causal_ranking
        if not ranking:
            print("  (no diagnosed failures)")
        for rank in ranking:
            print(
                "  {}  freq={}  impact={:.1f}%  z={:.2f}  conf={:.2f}".format(
                    rank.cause.label,
                    rank.frequency,
                    rank.fractional_impact * 100,
                    rank.z_score_impact,
                    rank.average_confidence,
                )
            )

    elif mode == "stability":
        isolated = DeterministicBoundary(cache_isolated=True, seed_control="cli_seed")
        stable = runner.run_repeated_evaluation(
            dataset, iterations=3, engine_factory=lambda _ctx, cb: _make_engine(cb), boundary=isolated
        )
        print(
            "Isolated   (cacheIsolated: True ): mean F1 {:.3f}  variance {:.5f}  — {}".format(
                stable.mean_f1, stable.f1_variance, stable.drift_fingerprint
            )
        )

        def unstable_engine(ctx, cb):
            tool_score = 0.95 if ctx.iteration % 2 == 0 else 0.30
            evaluator = AnyEquivalenceEvaluator(
                evaluator_identifier="drift",
                evaluator=lambda b, c: (
                    1.0
                    if b == c
                    else (0.0 if b.type_identifier != c.type_identifier else (tool_score if b.type_identifier == "tool" else 0.8))
                ),
            )
            config = AlignmentConfiguration(
                profile=AlignmentProfile.developer_debug_v1, equivalence_evaluator=evaluator
            )
            return TraceAlignmentEngine(
                config, capture_mode=VerificationCaptureMode.EVIDENCE_ONLY, meta_trace_callback=cb
            )

        unstable = runner.run_repeated_evaluation(
            dataset,
            iterations=4,
            engine_factory=unstable_engine,
            boundary=DeterministicBoundary(cache_isolated=False),
        )
        print(
            "Perturbed  (cacheIsolated: False): mean F1 {:.3f}  variance {:.5f}  — {}".format(
                unstable.mean_f1, unstable.f1_variance, unstable.drift_fingerprint
            )
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

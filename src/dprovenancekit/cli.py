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


def _run_gate(argv) -> int:
    """``dprovenancekit gate`` — fail when a candidate run regresses against a golden run.

    Server-less: loads both runs from a local WAL SQLite database (the same on-disk format
    the hosted backend uses) and runs the library's own :class:`RegressionGate`. Exit codes
    mirror ``server/dprov_gate.py``::

        0  no regression (gate passed)
        1  regression detected
        2  usage / run-not-found error
    """
    import argparse
    import json
    import sqlite3
    import uuid

    from .alignment_models import RegressionLevel
    from .event import AnyTraceableEvent
    from .sqlite_store import SQLiteTraceStore
    from .testing import RegressionGate

    ap = argparse.ArgumentParser(
        prog="dprovenancekit gate",
        description="Fail the build when a candidate run regresses against a golden run.",
    )
    ap.add_argument("--db", required=True, help="path to the SQLite trace database")
    ap.add_argument("--golden", required=True, help="golden (known-good) run id")
    ap.add_argument("--candidate", required=True, help="candidate run id to gate")
    ap.add_argument(
        "--max-level",
        default="none",
        choices=["none", "low", "medium", "high"],
        help="worst severity that still passes (default: none = strict)",
    )
    ap.add_argument(
        "--allow-divergent",
        action="store_true",
        help="tolerate per-step changes; gate only on severity",
    )
    ap.add_argument("--json", action="store_true", help="emit the report as JSON")
    args = ap.parse_args(argv)

    try:
        golden_id = uuid.UUID(args.golden)
        candidate_id = uuid.UUID(args.candidate)
    except ValueError:
        print("error: --golden/--candidate must be valid run ids (UUIDs)", file=sys.stderr)
        return 2

    try:
        store = SQLiteTraceStore(AnyTraceableEvent, args.db, start_writer=False)
    except (sqlite3.Error, OSError) as exc:
        print(f"error: could not open database {args.db}: {exc}", file=sys.stderr)
        return 2
    try:
        golden = store.get_run(golden_id)
        candidate = store.get_run(candidate_id)
    finally:
        store.close()

    not_found = [name for name, run in (("golden", golden), ("candidate", candidate)) if run is None]
    if not_found:
        print(f"error: run not found in {args.db}: {', '.join(not_found)}", file=sys.stderr)
        return 2

    report = RegressionGate(
        max_regression_level=RegressionLevel(args.max_level),
        allow_divergent_steps=args.allow_divergent,
    ).check(golden, candidate)

    if args.json:
        print(
            json.dumps(
                {
                    "passed": report.passed,
                    "regression_level": report.regression_level.value,
                    "strength": report.strength,
                    "max_regression_level": report.max_regression_level.value,
                    "allow_divergent_steps": report.allow_divergent_steps,
                    "fingerprint_match": report.fingerprint_match,
                    "golden_fingerprint": report.golden_fingerprint,
                    "candidate_fingerprint": report.candidate_fingerprint,
                    "steps_by_change": report.steps_by_change,
                    "reasoning": report.reasoning,
                    "summary": report.summary(),
                },
                indent=2,
            )
        )
    else:
        print(report.summary())

    return 0 if report.passed else 1


def _run_anomalies(argv) -> int:
    """``dprovenancekit anomalies`` — run anomaly rules over recorded runs.

    Loads rules from a JSON config and evaluates them against a local SQLite database, either
    over a single run (``--run``) or every run in the store. Exit codes::

        0  no anomalies
        1  anomalies found
        2  usage / config / run-not-found error
    """
    import argparse
    import json
    import sqlite3
    import uuid

    from .anomaly import AnomalyDetector
    from .event import AnyTraceableEvent
    from .rules import build_rules
    from .sqlite_store import SQLiteTraceStore

    ap = argparse.ArgumentParser(
        prog="dprovenancekit anomalies",
        description="Run out-of-the-box anomaly rules over recorded runs.",
    )
    ap.add_argument("--db", required=True, help="path to the SQLite trace database")
    ap.add_argument("--rules", required=True, help="path to a JSON rules config")
    ap.add_argument("--run", default=None, help="restrict to a single run id (default: all runs)")
    ap.add_argument("--json", action="store_true", help="emit the findings as JSON")
    args = ap.parse_args(argv)

    try:
        with open(args.rules, encoding="utf-8") as fh:
            config = json.load(fh)
        specs = config["rules"] if isinstance(config, dict) else config
        rules = build_rules(specs)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"error: could not load rules from {args.rules}: {exc}", file=sys.stderr)
        return 2

    run_id = None
    if args.run is not None:
        try:
            run_id = uuid.UUID(args.run)
        except ValueError:
            print("error: --run must be a valid run id (UUID)", file=sys.stderr)
            return 2

    try:
        store = SQLiteTraceStore(AnyTraceableEvent, args.db, start_writer=False)
    except (sqlite3.Error, OSError) as exc:
        print(f"error: could not open database {args.db}: {exc}", file=sys.stderr)
        return 2
    try:
        if run_id is not None:
            run = store.get_run(run_id)
            if run is None:
                print(f"error: run not found in {args.db}: {args.run}", file=sys.stderr)
                return 2
            found = [r.make_anomaly(run) for r in rules if r.anomaly_query.ast.evaluate(run)]
        else:
            found = AnomalyDetector(store).detect_anomalies(rules)
    finally:
        store.close()

    if args.json:
        print(
            json.dumps(
                {
                    "count": len(found),
                    "anomalies": [
                        {"rule": a.rule_name, "run_id": str(a.run_id), "description": a.description}
                        for a in found
                    ],
                },
                indent=2,
            )
        )
    elif not found:
        print("No anomalies detected.")
    else:
        print(f"{len(found)} anomaly(ies) detected:")
        for anomaly in found:
            print(f"  [{anomaly.rule_name}] {anomaly.description}")

    return 1 if found else 0


def main(argv=None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    if argv and argv[0] == "gate":
        return _run_gate(argv[1:])
    if argv and argv[0] == "anomalies":
        return _run_anomalies(argv[1:])

    print("DProvenanceKit CLI Evaluator")
    print("============================")

    mode = argv[0] if argv else "evaluate"
    if mode not in ("evaluate", "diagnose", "stability"):
        print("Usage: dprovenancekit <gate|anomalies|evaluate|diagnose|stability>")
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

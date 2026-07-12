"""Pin the curated public API surface.

``__all__`` is the semver-tracked contract. This test fails if it drifts, so any
addition or removal is a deliberate, reviewed change — not an accident. It also asserts
the non-breaking guarantee behind the July-2026 curation: symbols that were removed from
``__all__`` (internal machinery, Swift-port view/perturbation helpers) stay importable,
so ``from dprovenancekit import <name>`` keeps working for existing code.
"""

from __future__ import annotations

import importlib

import dprovenancekit

# The exact curated public surface. Update this set only alongside an intentional change
# to dprovenancekit/__init__.py::__all__.
EXPECTED_PUBLIC = {
    "TracePriority", "TraceableEvent", "TraceEvent", "AnyTraceableEvent",
    "TraceEdge", "TraceEdgeType", "TraceGraph", "TraceExplanation", "TraceDropStats",
    "BufferCapacity", "EvictionPolicy", "OfflineConfig",
    "TraceContext", "DProvenanceKit", "ActiveTraceRun",
    "TraceRun", "TraceQueryDSL",
    "TraceStore", "InMemoryTraceStore", "SQLiteTraceStore", "RawTraceStore",
    "RawTraceRun", "RawTraceEvent", "TraceError", "NodeNotFoundError",
    "IngestedRun", "OTelSpanEvent", "ingest_otlp", "run_id_for_trace",
    "Anomaly", "AnomalyRule", "AnomalyDetector", "ToolDropRule", "LoopingRule",
    "UnregisteredToolRule", "UnusedToolResultRule", "build_rule", "build_rules",
    "TraceDiffEngine", "TraceDiffResult", "Change", "ChangeKind",
    "TraceReplayEngine", "ReplaySnapshot", "ReplayManifest", "SpanNode", "SequenceGap",
    "SnapshotDiffEngine", "SnapshotDiffResult", "SpanChange", "SpanChangeKind",
    "EventChange", "EventChangeKind", "DivergencePoint", "DiffSummary",
    "AlignmentConfiguration", "AlignmentProfile", "AlignmentMode", "AlignmentStrategy",
    "AnyEquivalenceEvaluator", "TraceAlignmentEngine", "VerificationCaptureMode",
    "TraceAlignmentResult", "EventAlignment", "AlignmentState", "AlignmentStateKind",
    "AlignmentStrength", "AlignmentStrengthCategory", "RegressionRisk", "RegressionLevel",
    "AlignmentFinding", "AlignmentFindingKind",
    "BenchmarkRunner", "DProvenanceCorpus",
    "RegressionGate", "RegressionReport", "RegressionError", "assert_no_regression",
    "exact_equality_evaluator", "run_fingerprint",
    "render_report_html", "render_trace_html",
    "TracedEvent", "traced", "traced_run", "record_event",
    "trace",
}

# A representative sample of symbols removed from __all__ in the curation that must remain
# importable (the non-breaking guarantee — these are still bound at package root).
STILL_IMPORTABLE_INTERNALS = {
    "TraceQueryCompiler", "TraceQueryPlanner", "CompiledSQLQuery", "IndexConstraint",
    "TraceWriteBuffer", "SQLiteWriter", "SQLiteConnection",
    "TraceEventRow", "RunRow", "AnyActiveTraceRun", "NotImplementedTraceError",
    "LiveTraceQueryEngine", "TraceQuerySubscription", "QueryState", "LiveAnomalySubscription",
    "AlignmentEvidenceCollector", "NullEvidenceCollector", "DefaultTraceMatcher",
    "DefaultAlignmentInterpreter", "AlignmentNarrativeCompiler", "InterpretationStep",
    "FidelityVector", "CoverageInvariant", "NoHallucinationInvariant",
    "BenchmarkReport", "DeterministicBoundary",
    "EvaluationPerturbationLayer", "PerturbationMode",
    "SpanViewModel", "flatten_span_tree", "RenderHints", "DiffPresentationMode",
}


def test_public_api_matches_expected_surface():
    assert set(dprovenancekit.__all__) == EXPECTED_PUBLIC


def test_all_is_deduplicated():
    assert len(dprovenancekit.__all__) == len(set(dprovenancekit.__all__))


def test_every_public_name_is_bound():
    for name in dprovenancekit.__all__:
        assert hasattr(dprovenancekit, name), f"{name} in __all__ but not importable"


def test_curation_is_non_breaking_for_internals():
    for name in STILL_IMPORTABLE_INTERNALS:
        assert hasattr(dprovenancekit, name), (
            f"{name} was removed from __all__ but must stay importable "
            "(the curation is non-breaking)"
        )
        assert name not in dprovenancekit.__all__, (
            f"{name} is meant to be internal; if it is now public, move it to "
            "EXPECTED_PUBLIC deliberately"
        )


def test_star_import_binds_only_public_surface():
    ns: dict = {}
    exec("from dprovenancekit import *", ns)
    bound = {k for k in ns if not k.startswith("__")}
    assert bound == EXPECTED_PUBLIC

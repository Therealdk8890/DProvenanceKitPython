"""DProvenanceKit — reasoning observability and regression testing for AI systems.

A Python port of the Swift DProvenanceKit. Run → Record → Query → Diff → Detect
Regressions.

    kit = DProvenanceKit(MyEvent)
    store = InMemoryTraceStore()
    with kit.run(context_id="case-1", store=store):
        kit.record(MyEvent.document_evaluated("DocA", 0.95))
        kit.record(MyEvent.conflict_detected("timeline_inconsistency"))

    runs = store.query_runs(
        TraceQueryDSL().requiring_step("conflictDetected").missing_step("documentEvaluated")
    )
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError as _PackageNotFoundError
from importlib.metadata import version as _distribution_version

# Core event model
from .priority import TracePriority
from .event import (
    TraceableEvent,
    TraceEvent,
    TraceEventRow,
    RunRow,
    AnyTraceableEvent,
)
from .edge import TraceEdge, TraceEdgeType
from .graph import TraceGraph, TraceExplanation
from .drop_stats import TraceDropStats, TraceDropTally
from .config import BufferCapacity, EvictionPolicy, OfflineConfig

# Recording + context
from .context import TraceContext, AnyActiveTraceRun
from .kit import DProvenanceKit, ActiveTraceRun

# Query
from .query import (
    TraceRun,
    TraceQueryDSL,
    TraceQueryNode,
    TraceQueryPlanner,
    TraceQueryCompiler,
    CompiledSQLQuery,
    IndexConstraint,
)

# Buffer + stores
from .write_buffer import TraceWriteBuffer
from .store import (
    TraceStore,
    InMemoryTraceStore,
    TraceError,
    NodeNotFoundError,
    NotImplementedTraceError,
)
from .sqlite_store import SQLiteTraceStore, SQLiteConnection, SQLiteWriter
from .raw_store import RawTraceStore, RawTraceRun, RawTraceEvent

# OpenTelemetry ingestion
from .otel_ingest import IngestedRun, OTelSpanEvent, ingest_otlp, run_id_for_trace

# Live querying + anomalies
from .live_engine import LiveTraceQueryEngine, TraceQuerySubscription, QueryState
from .anomaly import Anomaly, AnomalyRule, AnomalyDetector, LiveAnomalySubscription
from .rules import (
    ToolDropRule,
    LoopingRule,
    UnregisteredToolRule,
    UnusedToolResultRule,
    build_rule,
    build_rules,
)

# Diff + replay
from .diff import TraceDiffEngine, TraceDiffResult, Change, ChangeKind
from .replay import (
    TraceReplayEngine,
    ReplaySnapshot,
    ReplayEvent,
    ReplaySource,
    ReplayManifest,
    ReplaySnapshotMetadata,
    SpanNode,
    SequenceGap,
)
from .snapshot_diff import (
    SnapshotDiffEngine,
    SnapshotDiffResult,
    SpanChange,
    SpanChangeKind,
    EventChange,
    EventChangeKind,
    DivergencePoint,
    DiffSummary,
)
from .render_hints import RenderHints, DiffPresentationMode

# Alignment
from .alignment_config import (
    AlignmentConfiguration,
    AlignmentProfile,
    AlignmentMode,
    AlignmentStrategy,
    AnyEquivalenceEvaluator,
)
from .alignment_engine import TraceAlignmentEngine, VerificationCaptureMode
from .alignment_models import (
    TraceAlignmentResult,
    EventAlignment,
    AlignmentState,
    AlignmentStateKind,
    AlignmentStrength,
    AlignmentStrengthCategory,
    AmbiguousMatch,
    AlignmentExplanation,
    HeuristicEvidence,
    HeuristicEvidenceCategory,
    RegressionRisk,
    RegressionLevel,
    AlignmentFinding,
    AlignmentFindingKind,
    DecisionTimelineEntry,
)
from .alignment_meta import AlignmentMetaEvent, MetaEventKind
from .alignment_contract import AlignmentExecutionContract
from .alignment_evidence import (
    AlignmentBinding,
    BindingDecision,
    EquivalenceDecisionRecord,
    EquivalenceReason,
    InterpretationStep,
    AlignmentEvidence,
    AlignmentEvidenceCollector,
    NullEvidenceCollector,
    EvidenceCollector,
    VerificationArtifacts,
)
from .alignment_semantics import EquivalenceDecision, DefaultEquivalenceModel
from .alignment_matcher import DefaultTraceMatcher
from .alignment_interpreter import DefaultAlignmentInterpreter
from .alignment_findings import AlignmentFindingsExtractor
from .alignment_narrative import AlignmentNarrativeCompiler
from .alignment_render import AlignmentRenderNode, RenderHint, render_models
from .alignment_snapshot import (
    AlignmentSnapshot,
    AlignmentSnapshotValidator,
    DriftToleranceMode,
    SnapshotValidationError,
)

# Verification
from .verification import (
    FidelityVector,
    FormalizationMap,
    DefaultFormalizationMapBuilder,
    CoverageInvariant,
    CompletenessInvariant,
    CausalOrderingInvariant,
    NoHallucinationInvariant,
    ExplainabilityAuditor,
    TraceGraphValidator,
    TraceGraphProvenanceValidator,
    TraceGraphValidationError,
    StructuralCycleDetected,
    SelfReferentialEdge,
)

# Benchmark + corpus
from .benchmark import (
    BenchmarkRunner,
    BenchmarkReport,
    BenchmarkCase,
    BenchmarkCaseResult,
    BenchmarkDataset,
    BenchmarkStabilityReport,
    BenchmarkDeltaReport,
    CategoryMetrics,
    CategoryDeltaMetrics,
    CausalRank,
    ExpectedFinding,
    DeterministicBoundary,
    EnvironmentContext,
    BenchmarkFailureDiagnoser,
    DiagnosedFailure,
    FailureCause,
    FailureSeverityProfile,
    SignalFailure,
    ModelFailure,
    SearchFailure,
    DataFailure,
)
from .corpus import DProvenanceCorpus
from .perturbation import EvaluationPerturbationLayer, PerturbationMode

# View models (pure logic)
from .viewmodel import SpanViewModel, FlattenedSpanNode, flatten_span_tree

# Regression-gate test helper
from .testing import (
    RegressionGate,
    RegressionReport,
    RegressionError,
    assert_no_regression,
    exact_equality_evaluator,
    run_fingerprint,
)
from .report import render_report_html
from .visualizer import render_trace_html

# Framework-agnostic instrumentation (decorators / context manager)
from .instrument import TracedEvent, traced, traced_run, record_event

# High-level Facade API
from .facade import trace

try:
    __version__ = _distribution_version("dprovenancekit")
except _PackageNotFoundError:
    # Source checkout without installed metadata; keep in sync with pyproject.toml.
    __version__ = "0.6.1"




# The supported public API. Every symbol imported above stays importable
# (``from dprovenancekit import X`` keeps working for anything, including the internal
# machinery — query compiler/planner, write buffer, sqlite connection/writer, the
# alignment engine's evidence/binding/interpreter internals, the benchmark report types,
# the verification invariants, and the Swift-port view/perturbation helpers). ``__all__``
# is deliberately narrower than that: it is the curated, semver-tracked surface —
# what ``from dprovenancekit import *`` binds and what tooling should treat as public.
# Keep additions here intentional; an internal symbol only belongs on this list once it
# is a contract we mean to keep.
__all__ = [
    # Event model
    "TracePriority",
    "TraceableEvent",
    "TraceEvent",
    "AnyTraceableEvent",
    "TraceEdge",
    "TraceEdgeType",
    "TraceGraph",
    "TraceExplanation",
    "TraceDropStats",
    "BufferCapacity",
    "EvictionPolicy",
    "OfflineConfig",
    # Recording + context
    "TraceContext",
    "DProvenanceKit",
    "ActiveTraceRun",
    # Query
    "TraceRun",
    "TraceQueryDSL",
    # Stores
    "TraceStore",
    "InMemoryTraceStore",
    "SQLiteTraceStore",
    "RawTraceStore",
    "RawTraceRun",
    "RawTraceEvent",
    "TraceError",
    "NodeNotFoundError",
    # OpenTelemetry ingestion
    "IngestedRun",
    "OTelSpanEvent",
    "ingest_otlp",
    "run_id_for_trace",
    # Anomaly detection + rule library
    "Anomaly",
    "AnomalyRule",
    "AnomalyDetector",
    "ToolDropRule",
    "LoopingRule",
    "UnregisteredToolRule",
    "UnusedToolResultRule",
    "build_rule",
    "build_rules",
    # Diff
    "TraceDiffEngine",
    "TraceDiffResult",
    "Change",
    "ChangeKind",
    # Replay
    "TraceReplayEngine",
    "ReplaySnapshot",
    "ReplayManifest",
    "SpanNode",
    "SequenceGap",
    # Snapshot diff
    "SnapshotDiffEngine",
    "SnapshotDiffResult",
    "SpanChange",
    "SpanChangeKind",
    "EventChange",
    "EventChangeKind",
    "DivergencePoint",
    "DiffSummary",
    # Alignment (config, engine, and the result/verdict types users read)
    "AlignmentConfiguration",
    "AlignmentProfile",
    "AlignmentMode",
    "AlignmentStrategy",
    "AnyEquivalenceEvaluator",
    "TraceAlignmentEngine",
    "VerificationCaptureMode",
    "TraceAlignmentResult",
    "EventAlignment",
    "AlignmentState",
    "AlignmentStateKind",
    "AlignmentStrength",
    "AlignmentStrengthCategory",
    "RegressionRisk",
    "RegressionLevel",
    "AlignmentFinding",
    "AlignmentFindingKind",
    # Benchmark corpus (entry points)
    "BenchmarkRunner",
    "DProvenanceCorpus",
    # Regression-gate test helpers
    "RegressionGate",
    "RegressionReport",
    "RegressionError",
    "assert_no_regression",
    "exact_equality_evaluator",
    "run_fingerprint",
    # Rendering
    "render_report_html",
    "render_trace_html",
    # Instrumentation (decorators / context manager)
    "TracedEvent",
    "traced",
    "traced_run",
    "record_event",
    # High-level facade
    "trace",
]

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
from .cloud_store import (
    CloudTraceStore,
    CloudWriter,
    CloudWriterError,
    FlushTimedOut,
    CloudTraceStoreError,
    UnsupportedSchemaError,
    ServerError,
    default_transport,
)
from .circuit_breaker import CircuitBreaker, CircuitState

# Live querying + anomalies
from .live_engine import LiveTraceQueryEngine, TraceQuerySubscription, QueryState
from .anomaly import Anomaly, AnomalyRule, AnomalyDetector, LiveAnomalySubscription
from .rules import ToolDropRule, LoopingRule, build_rule, build_rules

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

__version__ = "0.2.0"

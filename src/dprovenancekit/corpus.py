"""A public corpus of agent traces for demos, UI development, and benchmarking."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Tuple

from .alignment_config import AnyEquivalenceEvaluator
from .alignment_models import AlignmentFinding, RegressionLevel, RegressionRisk
from .benchmark import BenchmarkCase, BenchmarkDataset, ExpectedFinding
from .event import TraceEvent, TraceableEvent
from .priority import TracePriority
from .query import TraceRun


class _AgentKind(Enum):
    FILE_IO = "fileIO"
    TOOL_EXECUTION = "toolExecution"
    PLANNING = "planning"
    DECISION = "decision"


@dataclass(frozen=True)
class AgentEvent(TraceableEvent):
    kind: _AgentKind
    action: str = ""
    file: str = ""
    tool_name: str = ""
    params: str = ""
    hypothesis: str = ""

    @property
    def type_identifier(self) -> str:
        return {
            _AgentKind.FILE_IO: "fileIO",
            _AgentKind.TOOL_EXECUTION: "tool",
            _AgentKind.PLANNING: "planning",
            _AgentKind.DECISION: "decision",
        }[self.kind]

    @property
    def priority(self) -> TracePriority:
        if self.kind == _AgentKind.DECISION:
            return TracePriority.CRITICAL
        if self.kind in (_AgentKind.FILE_IO, _AgentKind.TOOL_EXECUTION):
            return TracePriority.STRUCTURAL
        return TracePriority.DIAGNOSTIC

    def to_dict(self) -> dict:
        return {
            "kind": self.kind.value,
            "action": self.action,
            "file": self.file,
            "tool_name": self.tool_name,
            "params": self.params,
            "hypothesis": self.hypothesis,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AgentEvent":
        return cls(
            kind=_AgentKind(data["kind"]),
            action=data.get("action", ""),
            file=data.get("file", ""),
            tool_name=data.get("tool_name", ""),
            params=data.get("params", ""),
            hypothesis=data.get("hypothesis", ""),
        )

    # Factories mirroring the Swift enum cases ----------------------------------

    @classmethod
    def file_io(cls, action: str, file: str) -> "AgentEvent":
        return cls(_AgentKind.FILE_IO, action=action, file=file)

    @classmethod
    def tool_execution(cls, tool_name: str, params: str) -> "AgentEvent":
        return cls(_AgentKind.TOOL_EXECUTION, tool_name=tool_name, params=params)

    @classmethod
    def planning(cls, hypothesis: str) -> "AgentEvent":
        return cls(_AgentKind.PLANNING, hypothesis=hypothesis)

    @classmethod
    def decision(cls, action: str) -> "AgentEvent":
        return cls(_AgentKind.DECISION, action=action)


def _ev(run_id, ctx, seq, span_id, parent_span_id, payload) -> TraceEvent:
    return TraceEvent(
        run_id=run_id,
        context_id=ctx,
        engine_name="Agent",
        schema_version=1,
        sequence=seq,
        span_id=span_id,
        parent_span_id=parent_span_id,
        payload=payload,
    )


class DProvenanceCorpus:
    AgentEvent = AgentEvent

    @staticmethod
    def standard_evaluator() -> AnyEquivalenceEvaluator:
        """Payload-aware evaluator shared by the demo app, the CLI, and benchmark tests."""

        def evaluator(b: AgentEvent, c: AgentEvent) -> float:
            if b == c:
                return 1.0  # identical => exact match, no finding emitted
            if b.type_identifier != c.type_identifier:
                return 0.0
            if b.kind == _AgentKind.TOOL_EXECUTION:
                return 0.95  # tool substitution
            if b.kind == _AgentKind.DECISION:
                return 0.8  # decision drift
            return 0.0  # distinct hypotheses / files are not equivalent

        return AnyEquivalenceEvaluator(
            evaluator_identifier="dprov_standard_semantics", evaluator=evaluator
        )

    # MARK: - Example 1: Coding Agent Regression --------------------------------

    @staticmethod
    def coding_agent_regression() -> Tuple[TraceRun, TraceRun]:
        run_a, run_b = uuid.uuid4(), uuid.uuid4()
        base = [
            _ev(run_a, "demo_1", 0, "s1", None, AgentEvent.file_io("read", "App.swift")),
            _ev(run_a, "demo_1", 1, "s1", None, AgentEvent.tool_execution("SearchDocs", "SwiftUI")),
            _ev(run_a, "demo_1", 2, "s1", None, AgentEvent.decision("ValidateAPI")),
            _ev(run_a, "demo_1", 3, "s1", None, AgentEvent.decision("GenerateFix")),
            _ev(run_a, "demo_1", 4, "s1", None, AgentEvent.tool_execution("VerifyFix", "build")),
        ]
        comp = [
            _ev(run_b, "demo_1", 0, "s2", None, AgentEvent.file_io("read", "App.swift")),
            _ev(run_b, "demo_1", 1, "s2", None, AgentEvent.decision("GenerateFix")),
        ]
        return (
            TraceRun(run_id=run_a, context_id="demo_1", events=base),
            TraceRun(run_id=run_b, context_id="demo_1", events=comp),
        )

    # MARK: - Example 2: Semantic Evolution -------------------------------------

    @staticmethod
    def semantic_evolution() -> Tuple[TraceRun, TraceRun]:
        run_a, run_b = uuid.uuid4(), uuid.uuid4()
        base = [_ev(run_a, "demo_2", 0, "s1", None, AgentEvent.tool_execution("SearchDocumentation", "REST"))]
        comp = [_ev(run_b, "demo_2", 0, "s2", None, AgentEvent.tool_execution("LookupAPIDocs", "REST"))]
        return (
            TraceRun(run_id=run_a, context_id="demo_2", events=base),
            TraceRun(run_id=run_b, context_id="demo_2", events=comp),
        )

    # MARK: - Example 3: Reordering ---------------------------------------------

    @staticmethod
    def reordering() -> Tuple[TraceRun, TraceRun]:
        run_a, run_b = uuid.uuid4(), uuid.uuid4()
        base = [
            _ev(run_a, "demo_3", 0, "s1", None, AgentEvent.file_io("read", "Config.swift")),
            _ev(run_a, "demo_3", 1, "s1", None, AgentEvent.tool_execution("SearchDocs", "Config API")),
            _ev(run_a, "demo_3", 2, "s1", None, AgentEvent.decision("GenerateFix")),
        ]
        comp = [
            _ev(run_b, "demo_3", 0, "s2", None, AgentEvent.tool_execution("SearchDocs", "Config API")),
            _ev(run_b, "demo_3", 1, "s2", None, AgentEvent.file_io("read", "Config.swift")),
            _ev(run_b, "demo_3", 2, "s2", None, AgentEvent.decision("GenerateFix")),
        ]
        return (
            TraceRun(run_id=run_a, context_id="demo_3", events=base),
            TraceRun(run_id=run_b, context_id="demo_3", events=comp),
        )

    # MARK: - Example 4: Branch Collapse ----------------------------------------

    @staticmethod
    def branch_collapse() -> Tuple[TraceRun, TraceRun]:
        run_a, run_b = uuid.uuid4(), uuid.uuid4()
        base = [
            _ev(run_a, "demo_4", 0, "s1", None, AgentEvent.decision("Investigate")),
            _ev(run_a, "demo_4", 1, "sA", "s1", AgentEvent.planning("A")),
            _ev(run_a, "demo_4", 2, "sB", "s1", AgentEvent.planning("B")),
            _ev(run_a, "demo_4", 3, "sC", "s1", AgentEvent.planning("C")),
        ]
        comp = [
            _ev(run_b, "demo_4", 0, "s2", None, AgentEvent.decision("Investigate")),
            _ev(run_b, "demo_4", 1, "sA2", "s2", AgentEvent.planning("A")),
            _ev(run_b, "demo_4", 2, "sC2", "s2", AgentEvent.planning("C")),
        ]
        return (
            TraceRun(run_id=run_a, context_id="demo_4", events=base),
            TraceRun(run_id=run_b, context_id="demo_4", events=comp),
        )

    # MARK: - Example 5: Meaning-Preserving Mutation (Caching) -------------------

    @staticmethod
    def caching_mutation() -> BenchmarkCase:
        run_a, run_b = uuid.uuid4(), uuid.uuid4()
        base = [
            _ev(run_a, "demo_5", 0, "s1", None, AgentEvent.decision("UserLogin")),
            _ev(run_a, "demo_5", 1, "s1", None, AgentEvent.tool_execution("FetchProfile", "user123")),
            _ev(run_a, "demo_5", 2, "s1", None, AgentEvent.decision("RenderDashboard")),
        ]
        comp = [
            _ev(run_b, "demo_5", 0, "s2", None, AgentEvent.decision("UserLogin")),
            _ev(run_b, "demo_5", 1, "s2", None, AgentEvent.tool_execution("FetchCachedProfile", "user123")),
            _ev(run_b, "demo_5", 2, "s2", None, AgentEvent.decision("RenderDashboard")),
        ]
        return BenchmarkCase(
            name="Meaning-Preserving Mutation",
            description="Replaces network fetch with cached fetch",
            base_run=TraceRun(run_id=run_a, context_id="demo_5", events=base),
            comparison_run=TraceRun(run_id=run_b, context_id="demo_5", events=comp),
            expected_findings=[
                ExpectedFinding(AlignmentFinding.semantic_evolution("tool", "tool"), 1.0)
            ],
        )

    # MARK: - Example 6: Non-Semantic Noise Injection ---------------------------

    @staticmethod
    def noise_injection() -> BenchmarkCase:
        run_a, run_b = uuid.uuid4(), uuid.uuid4()
        base = [
            _ev(run_a, "demo_6", 0, "s1", None, AgentEvent.tool_execution("FetchProfile", "")),
            _ev(run_a, "demo_6", 1, "s1", None, AgentEvent.decision("RenderDashboard")),
        ]
        comp = [
            _ev(run_b, "demo_6", 0, "s2", None, AgentEvent.tool_execution("FetchProfile", "")),
            _ev(run_b, "demo_6", 1, "s2", None, AgentEvent.planning("Log.Debug cache miss")),
            _ev(run_b, "demo_6", 2, "s2", None, AgentEvent.decision("RenderDashboard")),
        ]
        return BenchmarkCase(
            name="Noise Injection",
            description="Injects telemetry/logging events",
            base_run=TraceRun(run_id=run_a, context_id="demo_6", events=base),
            comparison_run=TraceRun(run_id=run_b, context_id="demo_6", events=comp),
            expected_findings=[],
        )

    # MARK: - Example 7: Semantic Drift -----------------------------------------

    @staticmethod
    def semantic_drift() -> BenchmarkCase:
        run_a, run_b = uuid.uuid4(), uuid.uuid4()
        base = [_ev(run_a, "demo_7", 0, "s1", None, AgentEvent.decision("PaymentAuthorization"))]
        comp = [_ev(run_b, "demo_7", 0, "s2", None, AgentEvent.decision("PaymentPrecheck"))]
        return BenchmarkCase(
            name="Semantic Drift",
            description="Substitution attack (Authorization vs Precheck)",
            base_run=TraceRun(run_id=run_a, context_id="demo_7", events=base),
            comparison_run=TraceRun(run_id=run_b, context_id="demo_7", events=comp),
            expected_findings=[
                ExpectedFinding(AlignmentFinding.semantic_evolution("decision", "decision"), 0.8)
            ],
        )

    # MARK: - Example 8: Degenerate Traces --------------------------------------

    @staticmethod
    def degenerate_traces() -> BenchmarkCase:
        run_a, run_b = uuid.uuid4(), uuid.uuid4()
        return BenchmarkCase(
            name="Degenerate Traces",
            description="Empty trace vs Empty trace",
            base_run=TraceRun(run_id=run_a, context_id="demo_8", events=[]),
            comparison_run=TraceRun(run_id=run_b, context_id="demo_8", events=[]),
            expected_findings=[],
        )

    @staticmethod
    def dataset() -> BenchmarkDataset:
        coding_base, coding_comp = DProvenanceCorpus.coding_agent_regression()
        sem_base, sem_comp = DProvenanceCorpus.semantic_evolution()
        reord_base, reord_comp = DProvenanceCorpus.reordering()
        branch_base, branch_comp = DProvenanceCorpus.branch_collapse()
        return BenchmarkDataset(
            name="DProvenance Standard Corpus",
            description="Official verification dataset for alignment algorithms",
            cases=[
                BenchmarkCase(
                    name="Coding Agent Regression",
                    description="A critical validation decision was skipped",
                    base_run=coding_base,
                    comparison_run=coding_comp,
                    expected_findings=[
                        ExpectedFinding(AlignmentFinding.critical_step_removed("decision")),
                        ExpectedFinding(
                            AlignmentFinding.regression_risk_finding(
                                RegressionRisk(
                                    RegressionLevel.HIGH, 0.95, "Critical reasoning steps removed: decision"
                                )
                            )
                        ),
                    ],
                ),
                BenchmarkCase(
                    name="Semantic Evolution",
                    description="Replaced SearchDocumentation with LookupAPIDocs",
                    base_run=sem_base,
                    comparison_run=sem_comp,
                    expected_findings=[
                        ExpectedFinding(AlignmentFinding.semantic_evolution("tool", "tool"))
                    ],
                ),
                BenchmarkCase(
                    name="Reordered Execution",
                    description="Functionally identical, different order",
                    base_run=reord_base,
                    comparison_run=reord_comp,
                    expected_findings=[
                        ExpectedFinding(AlignmentFinding.reordered_execution("fileIO", 0, 1)),
                        ExpectedFinding(AlignmentFinding.reordered_execution("tool", 1, 0)),
                    ],
                ),
                BenchmarkCase(
                    name="Branch Collapse",
                    description="Hypothesis B was dropped",
                    base_run=branch_base,
                    comparison_run=branch_comp,
                    expected_findings=[],
                ),
                DProvenanceCorpus.caching_mutation(),
                DProvenanceCorpus.noise_injection(),
                DProvenanceCorpus.semantic_drift(),
                DProvenanceCorpus.degenerate_traces(),
            ],
        )

    @staticmethod
    def adversarial_dataset() -> BenchmarkDataset:
        def run(ctx, span, payloads):
            rid = uuid.uuid4()
            events = [_ev(uuid.uuid4(), ctx, i, span, None, p) for i, p in enumerate(payloads)]
            return TraceRun(run_id=rid, context_id=ctx, events=events)

        return BenchmarkDataset(
            name="DProvenance Adversarial Robustness Suite",
            description="Stress tests for causal failure modes and semantic traps",
            cases=[
                BenchmarkCase(
                    name="Dependency Inversion Trap",
                    description="Swaps order of two dependent critical events",
                    base_run=run("adv_1", "s1", [AgentEvent.decision("CreateCustomer"), AgentEvent.decision("GenerateInvoice")]),
                    comparison_run=run("adv_1", "s2", [AgentEvent.decision("GenerateInvoice"), AgentEvent.decision("CreateCustomer")]),
                    expected_findings=[
                        ExpectedFinding(AlignmentFinding.reordered_execution("decision", 0, 1)),
                        ExpectedFinding(AlignmentFinding.reordered_execution("decision", 1, 0)),
                        ExpectedFinding(
                            AlignmentFinding.regression_risk_finding(
                                RegressionRisk(
                                    RegressionLevel.HIGH,
                                    1.0,
                                    "Critical reasoning steps reordered: decision, decision",
                                )
                            )
                        ),
                    ],
                ),
                BenchmarkCase(
                    name="Causal Ambiguity Trap",
                    description="Multiple identical events to confuse bipartite matching",
                    base_run=run("adv_2", "s1", [AgentEvent.tool_execution("Search", "A"), AgentEvent.tool_execution("Search", "A")]),
                    comparison_run=run("adv_2", "s2", [AgentEvent.tool_execution("Search", "A"), AgentEvent.tool_execution("Search", "A")]),
                    expected_findings=[],
                ),
                BenchmarkCase(
                    name="Partial Trace Truncation",
                    description="Trace drops off before final critical decision",
                    base_run=run("adv_3", "s1", [AgentEvent.decision("Auth"), AgentEvent.decision("Commit")]),
                    comparison_run=run("adv_3", "s2", [AgentEvent.decision("Auth")]),
                    expected_findings=[
                        ExpectedFinding(AlignmentFinding.critical_step_removed("decision")),
                        ExpectedFinding(
                            AlignmentFinding.regression_risk_finding(
                                RegressionRisk(
                                    RegressionLevel.HIGH, 0.95, "Critical reasoning steps removed: decision"
                                )
                            )
                        ),
                    ],
                ),
                BenchmarkCase(
                    name="Semantic Substitution Trap",
                    description="False friend equivalence: Cached vs Recompute",
                    base_run=run("adv_4", "s1", [AgentEvent.tool_execution("FetchUserProfile", "u1")]),
                    comparison_run=run("adv_4", "s2", [AgentEvent.tool_execution("RecomputeProfileFromEvents", "u1")]),
                    expected_findings=[
                        ExpectedFinding(AlignmentFinding.semantic_evolution("tool", "tool"), 0.8)
                    ],
                ),
                BenchmarkCase(
                    name="Multi-tool Semantic Collapse",
                    description="Two tools replaced by one overarching tool",
                    base_run=run("adv_5", "s1", [AgentEvent.tool_execution("GetLocation", ""), AgentEvent.tool_execution("GetWeather", "")]),
                    comparison_run=run("adv_5", "s2", [AgentEvent.tool_execution("GetLocationAndWeather", "")]),
                    expected_findings=[
                        ExpectedFinding(AlignmentFinding.semantic_evolution("tool", "tool"), 0.8)
                    ],
                ),
            ],
        )

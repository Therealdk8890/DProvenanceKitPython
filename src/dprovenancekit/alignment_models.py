"""Result and finding models for the alignment engine."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional

from .alignment_meta import AlignmentMetaEvent
from .event import TraceEvent

#: A normalized heuristic score in 0.0-1.0 — algorithmic match strength, not a probability.
AlignmentStrength = float


class AlignmentStrengthCategory(Enum):
    STRONG = "strong"
    MODERATE = "moderate"
    WEAK = "weak"
    REJECTED = "rejected"

    @staticmethod
    def from_strength(strength: float) -> "AlignmentStrengthCategory":
        if 0.90 <= strength <= 1.00:
            return AlignmentStrengthCategory.STRONG
        if 0.75 <= strength < 0.90:
            return AlignmentStrengthCategory.MODERATE
        if 0.50 <= strength < 0.75:
            return AlignmentStrengthCategory.WEAK
        return AlignmentStrengthCategory.REJECTED


class HeuristicEvidenceCategory(Enum):
    TYPE_MATCH = "typeMatch"
    PAYLOAD_SIMILARITY = "payloadSimilarity"
    STRUCTURAL_CONTEXT = "structuralContext"
    TEMPORAL_LOCALITY = "temporalLocality"
    SEMANTIC_EQUIVALENCE = "semanticEquivalence"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class HeuristicEvidence:
    category: HeuristicEvidenceCategory
    score_contribution: float
    description: str


def sort_evidence(evidence: List[HeuristicEvidence]) -> List[HeuristicEvidence]:
    """Canonical ordering: by score contribution desc, then category value."""
    return sorted(
        evidence, key=lambda e: (-e.score_contribution, e.category.value)
    )


@dataclass(frozen=True)
class AlignmentExplanation:
    primary_reason: str
    final_score: float
    ranked_evidence: List[HeuristicEvidence] = field(default_factory=list)

    def __post_init__(self):
        # Enforce deterministic sorting by contract.
        object.__setattr__(self, "ranked_evidence", sort_evidence(list(self.ranked_evidence)))

    @staticmethod
    def none() -> "AlignmentExplanation":
        return AlignmentExplanation(primary_reason="No match", final_score=0.0, ranked_evidence=[])


class RegressionLevel(Enum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True)
class RegressionRisk:
    level: RegressionLevel
    strength: float
    reasoning: str


class AlignmentStateKind(Enum):
    EXACT_MATCH = "exactMatch"
    SEMANTIC_MATCH = "semanticMatch"
    REORDERED = "reordered"
    AMBIGUOUS = "ambiguous"
    ADDED = "added"
    REMOVED = "removed"


@dataclass(frozen=True)
class AlignmentState:
    kind: AlignmentStateKind
    strength: Optional[float] = None
    original_sequence: Optional[int] = None
    new_sequence: Optional[int] = None
    options_count: Optional[int] = None

    @staticmethod
    def exact_match():
        return AlignmentState(AlignmentStateKind.EXACT_MATCH)

    @staticmethod
    def semantic_match(strength):
        return AlignmentState(AlignmentStateKind.SEMANTIC_MATCH, strength=strength)

    @staticmethod
    def reordered(original_sequence, new_sequence):
        return AlignmentState(
            AlignmentStateKind.REORDERED,
            original_sequence=original_sequence,
            new_sequence=new_sequence,
        )

    @staticmethod
    def ambiguous(options_count):
        return AlignmentState(AlignmentStateKind.AMBIGUOUS, options_count=options_count)

    @staticmethod
    def added():
        return AlignmentState(AlignmentStateKind.ADDED)

    @staticmethod
    def removed():
        return AlignmentState(AlignmentStateKind.REMOVED)

    @property
    def is_removed(self) -> bool:
        return self.kind == AlignmentStateKind.REMOVED

    @property
    def is_semantic_match(self) -> bool:
        return self.kind == AlignmentStateKind.SEMANTIC_MATCH

    @property
    def is_exact_match(self) -> bool:
        return self.kind == AlignmentStateKind.EXACT_MATCH

    def __str__(self) -> str:
        # Mirrors Swift `String(describing:)` enough for the fidelity invariants, which
        # check the `reordered` / `ambiguous` prefixes of the interpretation's outputState.
        if self.kind == AlignmentStateKind.EXACT_MATCH:
            return "exactMatch"
        if self.kind == AlignmentStateKind.SEMANTIC_MATCH:
            return f"semanticMatch(strength: {self.strength})"
        if self.kind == AlignmentStateKind.REORDERED:
            return f"reordered(originalSequence: {self.original_sequence}, newSequence: {self.new_sequence})"
        if self.kind == AlignmentStateKind.AMBIGUOUS:
            return f"ambiguous(optionsCount: {self.options_count})"
        if self.kind == AlignmentStateKind.ADDED:
            return "added"
        return "removed"


@dataclass(frozen=True)
class AmbiguousMatch:
    event: TraceEvent
    strength: float
    explanation: AlignmentExplanation


@dataclass(frozen=True)
class EventAlignment:
    state: AlignmentState
    base_event: Optional[TraceEvent]
    comparison_event: Optional[TraceEvent]
    explanation: AlignmentExplanation
    ambiguous_candidates: List[AmbiguousMatch] = field(default_factory=list)


class AlignmentFindingKind(Enum):
    CRITICAL_STEP_REMOVED = "criticalStepRemoved"
    CRITICAL_STEP_ADDED = "criticalStepAdded"
    SEMANTIC_EVOLUTION = "semanticEvolution"
    REORDERED_EXECUTION = "reorderedExecution"
    AMBIGUITY_DETECTED = "ambiguityDetected"
    REGRESSION_RISK = "regressionRisk"


@dataclass(frozen=True)
class AlignmentFinding:
    kind: AlignmentFindingKind
    base_identifier: Optional[str] = None
    comp_identifier: Optional[str] = None
    original_sequence: Optional[int] = None
    new_sequence: Optional[int] = None
    options_count: Optional[int] = None
    regression_risk: Optional[RegressionRisk] = None

    @staticmethod
    def critical_step_removed(base_event_identifier):
        return AlignmentFinding(
            AlignmentFindingKind.CRITICAL_STEP_REMOVED, base_identifier=base_event_identifier
        )

    @staticmethod
    def critical_step_added(comp_event_identifier):
        return AlignmentFinding(
            AlignmentFindingKind.CRITICAL_STEP_ADDED, comp_identifier=comp_event_identifier
        )

    @staticmethod
    def semantic_evolution(base_identifier, comp_identifier):
        return AlignmentFinding(
            AlignmentFindingKind.SEMANTIC_EVOLUTION,
            base_identifier=base_identifier,
            comp_identifier=comp_identifier,
        )

    @staticmethod
    def reordered_execution(event_identifier, original_sequence, new_sequence):
        return AlignmentFinding(
            AlignmentFindingKind.REORDERED_EXECUTION,
            base_identifier=event_identifier,
            original_sequence=original_sequence,
            new_sequence=new_sequence,
        )

    @staticmethod
    def ambiguity_detected(event_identifier, options_count):
        return AlignmentFinding(
            AlignmentFindingKind.AMBIGUITY_DETECTED,
            base_identifier=event_identifier,
            options_count=options_count,
        )

    @staticmethod
    def regression_risk_finding(risk: RegressionRisk):
        return AlignmentFinding(AlignmentFindingKind.REGRESSION_RISK, regression_risk=risk)

    @property
    def category_name(self) -> str:
        return {
            AlignmentFindingKind.CRITICAL_STEP_REMOVED: "CriticalStepRemoved",
            AlignmentFindingKind.CRITICAL_STEP_ADDED: "CriticalStepAdded",
            AlignmentFindingKind.SEMANTIC_EVOLUTION: "SemanticMatch",
            AlignmentFindingKind.REORDERED_EXECUTION: "ReorderedExecution",
            AlignmentFindingKind.AMBIGUITY_DETECTED: "AmbiguityDetected",
            AlignmentFindingKind.REGRESSION_RISK: "RegressionRisk",
        }[self.kind]


@dataclass(frozen=True)
class DecisionTimelineEntry:
    timestamp: float
    title: str
    detail: str
    strength_category: Optional[AlignmentStrengthCategory] = None
    meta_event: Optional[AlignmentMetaEvent] = None
    id: uuid.UUID = field(default_factory=uuid.uuid4)


@dataclass(frozen=True)
class TraceAlignmentResult:
    base_run_id: uuid.UUID
    comparison_run_id: uuid.UUID
    profile_hash: str
    engine_version: str
    alignments: List[EventAlignment]
    regression_risk: RegressionRisk
    verification_artifacts: Optional["object"] = None  # VerificationArtifacts

    def render_models(self):
        from .alignment_render import render_models as _render

        return _render(self)


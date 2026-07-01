"""Alignment profiles, the pluggable equivalence evaluator, and the configuration."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable

from .alignment_contract import AlignmentExecutionContract


class AlignmentMode(Enum):
    LINEAR = "linear"
    SPAN_AWARE = "spanAware"
    FULL_GRAPH = "fullGraph"


class AlignmentStrategy(Enum):
    STRICT_AUDIT = "strict_audit"
    DEVELOPER_DEBUG = "developer_debug"
    SEMANTIC_EXPLORATION = "semantic_exploration"


@dataclass(frozen=True)
class AlignmentProfile:
    strategy: AlignmentStrategy
    version: int
    type_weight: float
    payload_weight: float
    structural_weight: float
    temporal_weight: float
    semantic_threshold: float
    max_ambiguous_candidates: int
    ambiguity_delta_threshold: float
    alignment_mode: AlignmentMode


AlignmentProfile.strict_audit_v1 = AlignmentProfile(  # type: ignore[attr-defined]
    strategy=AlignmentStrategy.STRICT_AUDIT,
    version=1,
    type_weight=0.5,
    payload_weight=0.5,
    structural_weight=0.0,
    temporal_weight=0.0,
    semantic_threshold=0.99,
    max_ambiguous_candidates=1,
    ambiguity_delta_threshold=0.0,
    alignment_mode=AlignmentMode.LINEAR,
)

AlignmentProfile.developer_debug_v1 = AlignmentProfile(  # type: ignore[attr-defined]
    strategy=AlignmentStrategy.DEVELOPER_DEBUG,
    version=1,
    type_weight=0.4,
    payload_weight=0.4,
    structural_weight=0.15,
    temporal_weight=0.05,
    semantic_threshold=0.75,
    max_ambiguous_candidates=3,
    ambiguity_delta_threshold=0.10,
    alignment_mode=AlignmentMode.SPAN_AWARE,
)


@dataclass(frozen=True)
class AnyEquivalenceEvaluator:
    """A type-erased equivalence evaluator. ``evaluator`` scores two payloads in 0..1;
    ``ambiguity_threshold_fn`` returns the per-event ambiguity floor (default 0.4)."""

    evaluator_identifier: str
    evaluator: Callable
    ambiguity_threshold_fn: Callable = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.ambiguity_threshold_fn is None:
            object.__setattr__(self, "ambiguity_threshold_fn", lambda _e: 0.4)

    def evaluate_similarity(self, base, comparison) -> float:
        return self.evaluator(base, comparison)

    def ambiguity_threshold(self, event) -> float:
        return self.ambiguity_threshold_fn(event)


@dataclass(frozen=True)
class AlignmentConfiguration:
    profile: AlignmentProfile
    equivalence_evaluator: AnyEquivalenceEvaluator
    engine_version: str = "1.0.0"

    @property
    def profile_hash(self) -> str:
        return AlignmentExecutionContract.compute_profile_hash(
            profile=self.profile,
            evaluator_identifier=self.equivalence_evaluator.evaluator_identifier,
            engine_version=self.engine_version,
        )

    def score_match(self, base, comp):
        """Weighted heuristic score over type / payload / structure / temporal locality.

        Returns ``(score, AlignmentExplanation)``.
        """
        from .alignment_models import (
            AlignmentExplanation,
            HeuristicEvidence,
            HeuristicEvidenceCategory,
        )

        profile = self.profile
        score = 0.0
        evidence = []
        primary_reason = ""

        # 1. Type match.
        type_sim = 1.0 if base.payload.type_identifier == comp.payload.type_identifier else 0.0
        type_contribution = type_sim * profile.type_weight
        score += type_contribution
        if type_contribution > 0:
            evidence.append(
                HeuristicEvidence(
                    HeuristicEvidenceCategory.TYPE_MATCH,
                    type_contribution,
                    f"Type match ({base.payload.type_identifier})",
                )
            )
            primary_reason = "Exact Type Match"

        # 2. Payload similarity.
        payload_sim = self.equivalence_evaluator.evaluate_similarity(base.payload, comp.payload)
        payload_contribution = payload_sim * profile.payload_weight
        score += payload_contribution
        if payload_contribution > 0:
            evidence.append(
                HeuristicEvidence(
                    HeuristicEvidenceCategory.PAYLOAD_SIMILARITY,
                    payload_contribution,
                    f"Semantic equivalence score: {payload_sim:.2f}",
                )
            )
            if not primary_reason:
                primary_reason = "Semantic Payload Match"

        # 3. Structural context (span awareness).
        structural_sim = 0.0
        if profile.alignment_mode != AlignmentMode.LINEAR:
            if base.parent_span_id == comp.parent_span_id and base.parent_span_id is not None:
                structural_sim = 1.0
            elif base.parent_span_id is None and comp.parent_span_id is None:
                structural_sim = 1.0
        structural_contribution = structural_sim * profile.structural_weight
        score += structural_contribution
        if structural_contribution > 0:
            evidence.append(
                HeuristicEvidence(
                    HeuristicEvidenceCategory.STRUCTURAL_CONTEXT,
                    structural_contribution,
                    "Parent span matched",
                )
            )

        # 4. Temporal locality (rough heuristic on sequence index distance).
        seq_diff = abs(int(base.sequence) - int(comp.sequence))
        temp_sim = max(0.0, 1.0 - (seq_diff / 10.0))
        temp_contribution = temp_sim * profile.temporal_weight
        score += temp_contribution
        if temp_contribution > 0:
            evidence.append(
                HeuristicEvidence(
                    HeuristicEvidenceCategory.TEMPORAL_LOCALITY,
                    temp_contribution,
                    f"Temporal locality (+/-{seq_diff} events)",
                )
            )

        if not primary_reason:
            primary_reason = "Low Confidence Match"

        explanation = AlignmentExplanation(
            primary_reason=primary_reason, final_score=score, ranked_evidence=evidence
        )
        return score, explanation

# git-blob-rewrite

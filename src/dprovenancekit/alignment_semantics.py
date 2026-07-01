"""The deterministic, threshold-based equivalence model."""

from __future__ import annotations

from dataclasses import dataclass

from .alignment_evidence import (
    EquivalenceDecisionRecord,
    EquivalenceReason,
    EvidenceCollector,
)


@dataclass(frozen=True)
class EquivalenceDecision:
    equivalent: bool
    confidence: float
    reason: EquivalenceReason


class DefaultEquivalenceModel:
    def __init__(self, configuration):
        self.configuration = configuration

    def evaluate(self, a, b, evidence_collector: EvidenceCollector) -> EquivalenceDecision:
        score, explanation = self.configuration.score_match(a, b)
        is_equivalent = score >= self.configuration.profile.semantic_threshold

        decision = EquivalenceDecision(
            equivalent=is_equivalent,
            confidence=score,
            reason=EquivalenceReason(description=explanation.primary_reason),
        )

        evidence_collector.record_equivalence(
            EquivalenceDecisionRecord(
                lhs=str(a.id),
                rhs=str(b.id),
                confidence=score,
                equivalent=is_equivalent,
                reason=decision.reason,
            )
        )
        return decision

# git-blob-rewrite

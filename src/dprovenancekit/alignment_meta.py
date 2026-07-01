"""The observable meta-trace emitted by the alignment interpreter."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .event import TraceableEvent
from .priority import TracePriority


class MetaEventKind(Enum):
    EVALUATED_PAIR = "evaluatedPair"
    AMBIGUITY_THRESHOLD_MET = "ambiguityThresholdMet"
    CANDIDATE_EVICTED = "candidateEvicted"
    REGRESSION_DETECTED = "regressionDetected"


@dataclass(frozen=True)
class AlignmentMetaEvent(TraceableEvent):
    kind: MetaEventKind
    causal_parent_id: Optional[str] = None
    decision_node_id: str = ""
    base_sequence: Optional[int] = None
    comp_sequence: Optional[int] = None
    score: Optional[float] = None
    reason: Optional[str] = None
    level: Optional[str] = None
    reasoning: Optional[str] = None

    @property
    def type_identifier(self) -> str:
        return self.kind.value

    @property
    def priority(self) -> TracePriority:
        return TracePriority.STRUCTURAL

    # Factories mirroring the Swift enum cases ----------------------------------

    @classmethod
    def evaluated_pair(
        cls, causal_parent_id, decision_node_id, base_sequence, comp_sequence, score
    ):
        return cls(
            MetaEventKind.EVALUATED_PAIR,
            causal_parent_id=causal_parent_id,
            decision_node_id=decision_node_id,
            base_sequence=base_sequence,
            comp_sequence=comp_sequence,
            score=score,
        )

    @classmethod
    def ambiguity_threshold_met(
        cls, causal_parent_id, decision_node_id, comp_sequence, score
    ):
        return cls(
            MetaEventKind.AMBIGUITY_THRESHOLD_MET,
            causal_parent_id=causal_parent_id,
            decision_node_id=decision_node_id,
            comp_sequence=comp_sequence,
            score=score,
        )

    @classmethod
    def candidate_evicted(
        cls, causal_parent_id, decision_node_id, comp_sequence, reason
    ):
        return cls(
            MetaEventKind.CANDIDATE_EVICTED,
            causal_parent_id=causal_parent_id,
            decision_node_id=decision_node_id,
            comp_sequence=comp_sequence,
            reason=reason,
        )

    @classmethod
    def regression_detected(cls, causal_parent_id, decision_node_id, level, reasoning):
        return cls(
            MetaEventKind.REGRESSION_DETECTED,
            causal_parent_id=causal_parent_id,
            decision_node_id=decision_node_id,
            level=level,
            reasoning=reasoning,
        )

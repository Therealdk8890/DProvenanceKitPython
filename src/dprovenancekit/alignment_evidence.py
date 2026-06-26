"""Auditable evidence emitted during alignment: bindings, equivalence decisions, steps."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import List, Optional


@dataclass(frozen=True)
class EquivalenceReason:
    description: str


@dataclass(frozen=True)
class AlignmentBinding:
    base_event_id: "object"  # uuid.UUID
    comparison_event_id: "object"
    similarity_score: float


@dataclass(frozen=True)
class BindingDecision:
    base_id: str
    comparison_id: str
    similarity_score: float


@dataclass(frozen=True)
class EquivalenceDecisionRecord:
    lhs: str
    rhs: str
    confidence: float
    equivalent: bool
    reason: EquivalenceReason


@dataclass(frozen=True)
class InterpretationStep:
    source_binding: Optional[AlignmentBinding]
    base_id: Optional[str]
    comparison_id: Optional[str]
    output_state: str
    rationale: str
    base_sequence: Optional[int] = None
    comparison_sequence: Optional[int] = None


@dataclass(frozen=True)
class AlignmentEvidence:
    bindings: List[BindingDecision]
    equivalence_decisions: List[EquivalenceDecisionRecord]
    interpretation_steps: List[InterpretationStep]


@dataclass(frozen=True)
class VerificationArtifacts:
    evidence: AlignmentEvidence


class EvidenceCollector:
    def record_binding(self, decision: BindingDecision) -> None: ...
    def record_equivalence(self, record: EquivalenceDecisionRecord) -> None: ...
    def record_interpretation(self, step: InterpretationStep) -> None: ...


class NullEvidenceCollector(EvidenceCollector):
    def record_binding(self, decision: BindingDecision) -> None:
        pass

    def record_equivalence(self, record: EquivalenceDecisionRecord) -> None:
        pass

    def record_interpretation(self, step: InterpretationStep) -> None:
        pass


class AlignmentEvidenceCollector(EvidenceCollector):
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._bindings: List[BindingDecision] = []
        self._equivalence_decisions: List[EquivalenceDecisionRecord] = []
        self._interpretation_steps: List[InterpretationStep] = []

    def record_binding(self, decision: BindingDecision) -> None:
        with self._lock:
            self._bindings.append(decision)

    def record_equivalence(self, record: EquivalenceDecisionRecord) -> None:
        with self._lock:
            self._equivalence_decisions.append(record)

    def record_interpretation(self, step: InterpretationStep) -> None:
        with self._lock:
            self._interpretation_steps.append(step)

    def export_evidence(self) -> AlignmentEvidence:
        with self._lock:
            return AlignmentEvidence(
                bindings=list(self._bindings),
                equivalence_decisions=list(self._equivalence_decisions),
                interpretation_steps=list(self._interpretation_steps),
            )

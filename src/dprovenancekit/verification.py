"""Explainability fidelity invariants and trace-graph validators.

The invariants audit the matcher → semantics → interpretation chain: every reported
match must be grounded in a binding (coverage), evaluated by the semantics layer
(completeness), supported by an equivalent decision (no hallucination), and any execution
reordering must be faithfully *reported* as such (causal ordering).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Dict, List

from .alignment_evidence import (
    AlignmentEvidence,
    BindingDecision,
    EquivalenceDecisionRecord,
    InterpretationStep,
)
from .graph import TraceGraph

_SEP = "\x01"


@dataclass(frozen=True)
class FidelityVector:
    coverage: float
    completeness: float
    causal_ordering: float
    no_hallucinations: float

    @property
    def overall_score(self) -> float:
        return (self.coverage + self.completeness + self.causal_ordering + self.no_hallucinations) / 4.0


@dataclass(frozen=True)
class FormalizationMap:
    bindings: List[BindingDecision]
    decisions: List[EquivalenceDecisionRecord]
    interpretations: List[InterpretationStep]


class DefaultFormalizationMapBuilder:
    def build(self, evidence: AlignmentEvidence) -> FormalizationMap:
        return FormalizationMap(
            bindings=evidence.bindings,
            decisions=evidence.equivalence_decisions,
            interpretations=evidence.interpretation_steps,
        )


class CoverageInvariant:
    """Every reported match must be grounded in a recorded binding."""

    def evaluate(self, m: FormalizationMap) -> float:
        matched = [s for s in m.interpretations if s.base_id is not None and s.comparison_id is not None]
        if not matched:
            return 1.0
        bound_pairs = {f"{b.base_id}{_SEP}{b.comparison_id}" for b in m.bindings}
        grounded = sum(1 for s in matched if f"{s.base_id}{_SEP}{s.comparison_id}" in bound_pairs)
        return grounded / len(matched)


class CompletenessInvariant:
    """Every reported alignment must have been evaluated by the semantics layer."""

    def evaluate(self, m: FormalizationMap) -> float:
        matched = [s for s in m.interpretations if s.base_id is not None and s.comparison_id is not None]
        if not matched:
            return 1.0
        decision_pairs = {f"{d.lhs}{_SEP}{d.rhs}" for d in m.decisions}
        evaluated = sum(1 for s in matched if f"{s.base_id}{_SEP}{s.comparison_id}" in decision_pairs)
        return evaluated / len(matched)


class CausalOrderingInvariant:
    """Every matched pair whose relative execution order changed must be *reported* as
    reordered. A faithful explanation that labels its reorders scores 1.0 even when the
    trace is heavily reordered."""

    def evaluate(self, m: FormalizationMap) -> float:
        matched = [
            s
            for s in m.interpretations
            if s.base_sequence is not None and s.comparison_sequence is not None
        ]
        matched.sort(key=lambda s: s.base_sequence)
        if len(matched) < 2:
            return 1.0

        out_of_order = set()
        for i in range(len(matched)):
            for j in range(i + 1, len(matched)):
                if matched[i].comparison_sequence > matched[j].comparison_sequence:
                    out_of_order.add(i)
                    out_of_order.add(j)

        unreported = sum(1 for idx in out_of_order if not matched[idx].output_state.startswith("reordered"))
        return 1.0 - (unreported / len(matched))


class NoHallucinationInvariant:
    """Each definitive match claim must agree with its own equivalence decision.
    Ambiguous verdicts are exempt — ambiguity is an honest 'not confident' outcome."""

    def evaluate(self, m: FormalizationMap) -> float:
        matched = [s for s in m.interpretations if s.base_id is not None and s.comparison_id is not None]
        if not matched:
            return 1.0
        equivalent_by_pair: Dict[str, bool] = {}
        for d in m.decisions:
            equivalent_by_pair[f"{d.lhs}{_SEP}{d.rhs}"] = d.equivalent

        def supported(step) -> bool:
            if step.output_state.startswith("ambiguous"):
                return True
            return equivalent_by_pair.get(f"{step.base_id}{_SEP}{step.comparison_id}") is True

        return sum(1 for s in matched if supported(s)) / len(matched)


class ExplainabilityAuditor:
    def __init__(self, coverage=None, completeness=None, ordering=None, hallucination=None):
        self._coverage = coverage or CoverageInvariant()
        self._completeness = completeness or CompletenessInvariant()
        self._ordering = ordering or CausalOrderingInvariant()
        self._hallucination = hallucination or NoHallucinationInvariant()

    def audit(self, m: FormalizationMap) -> FidelityVector:
        return FidelityVector(
            coverage=self._coverage.evaluate(m),
            completeness=self._completeness.evaluate(m),
            causal_ordering=self._ordering.evaluate(m),
            no_hallucinations=self._hallucination.evaluate(m),
        )


# MARK: - Graph validators -------------------------------------------------------


class TraceGraphValidationError(Exception):
    pass


class StructuralCycleDetected(TraceGraphValidationError):
    def __init__(self, path: List[uuid.UUID]):
        super().__init__("Structural cycle detected in path: " + " -> ".join(str(p) for p in path))
        self.path = path


class SelfReferentialEdge(TraceGraphValidationError):
    def __init__(self, edge_id: uuid.UUID):
        super().__init__(f"Self-referential edge detected: {edge_id}")
        self.edge_id = edge_id


class TraceGraphValidator:
    def validate_structural_integrity(self, graph: TraceGraph) -> None:
        from .edge import TraceEdgeType

        # 1. Self-referential edges.
        for edge in graph.edges:
            if edge.source_id == edge.target_id:
                raise SelfReferentialEdge(edge.source_id)

        # 2. Cycle detection on causal edges (derivedFrom, generatedFrom).
        causal = [
            e for e in graph.edges
            if e.type in (TraceEdgeType.DERIVED_FROM, TraceEdgeType.GENERATED_FROM)
        ]
        adjacency: Dict[uuid.UUID, List[uuid.UUID]] = {}
        for edge in causal:
            adjacency.setdefault(edge.source_id, []).append(edge.target_id)

        visited = set()
        rec_stack = set()
        path: List[uuid.UUID] = []

        def has_cycle(node: uuid.UUID) -> None:
            visited.add(node)
            rec_stack.add(node)
            path.append(node)
            for neighbor in adjacency.get(node, []):
                if neighbor not in visited:
                    has_cycle(neighbor)
                elif neighbor in rec_stack:
                    path.append(neighbor)
                    raise StructuralCycleDetected(list(path))
            rec_stack.discard(node)
            path.pop()

        for node in graph.nodes.keys():
            if node not in visited:
                has_cycle(node)


class TraceGraphProvenanceValidator:
    def __init__(self, generated_section_identifier: str, fact_extracted_identifier: str):
        self.generated_section_identifier = generated_section_identifier
        self.fact_extracted_identifier = fact_extracted_identifier

    def detect_anomalies(self, graph: TraceGraph) -> List[str]:
        anomalies: List[str] = []

        generated_sections = [
            n for n in graph.nodes.values()
            if n.payload.type_identifier == self.generated_section_identifier
        ]
        for section in generated_sections:
            incoming = [e for e in graph.edges if e.target_id == section.id]
            if not incoming:
                anomalies.append(
                    f"Orphan generated section: {section.id} ({section.payload!r}). "
                    "No incoming evidence edges."
                )

        fact_nodes = [
            n for n in graph.nodes.values()
            if n.payload.type_identifier == self.fact_extracted_identifier
        ]
        for fact in fact_nodes:
            outgoing = [e for e in graph.edges if e.source_id == fact.id]
            if not outgoing:
                anomalies.append(
                    f"Unused extracted fact: {fact.id} ({fact.payload!r}). "
                    "Extracted but never informed a downstream section."
                )

        return anomalies

# git-blob-rewrite

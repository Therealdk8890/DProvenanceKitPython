"""The global, score-ordered bipartite matcher."""

from __future__ import annotations

from typing import List

from .alignment_evidence import AlignmentBinding, BindingDecision, EvidenceCollector


class DefaultTraceMatcher:
    def __init__(self, configuration):
        self.configuration = configuration

    def match(self, base, comparison, evidence_collector: EvidenceCollector) -> List[AlignmentBinding]:
        config = self.configuration

        # Score every candidate pair that clears the (per-base) ambiguity threshold, then
        # assign greedily HIGHEST SCORE FIRST so an exact/strong match always wins the
        # binding over a weaker incidental one.
        candidates = []  # (base_idx, comp_idx, score)
        for i, b_event in enumerate(base):
            threshold = config.equivalence_evaluator.ambiguity_threshold(b_event.payload)
            for j, c_event in enumerate(comparison):
                score, _ = config.score_match(b_event, c_event)
                if score >= threshold:
                    candidates.append((i, j, score))

        # Deterministic ordering: score desc, then base index, then comparison index.
        candidates.sort(key=lambda c: (-c[2], c[0], c[1]))

        bindings: List[AlignmentBinding] = []
        used_base = set()
        used_comp = set()
        for base_idx, comp_idx, score in candidates:
            if base_idx in used_base or comp_idx in used_comp:
                continue
            used_base.add(base_idx)
            used_comp.add(comp_idx)

            b_event = base[base_idx]
            c_event = comparison[comp_idx]
            bindings.append(
                AlignmentBinding(
                    base_event_id=b_event.id,
                    comparison_event_id=c_event.id,
                    similarity_score=score,
                )
            )
            evidence_collector.record_binding(
                BindingDecision(
                    base_id=str(b_event.id),
                    comparison_id=str(c_event.id),
                    similarity_score=score,
                )
            )

        return bindings

"""Turns bindings + equivalence decisions into explained, ordered alignments."""

from __future__ import annotations

import uuid
from typing import Callable, List, Optional

from .alignment_contract import AlignmentExecutionContract
from .alignment_config import AlignmentMode
from .alignment_evidence import EvidenceCollector, InterpretationStep
from .alignment_meta import AlignmentMetaEvent
from .alignment_models import (
    AlignmentState,
    AmbiguousMatch,
    EventAlignment,
)
from .event import TraceEvent


class DefaultAlignmentInterpreter:
    def __init__(self, configuration, meta_trace_callback: Optional[Callable] = None):
        self.configuration = configuration
        self.meta_trace_callback = meta_trace_callback

    def interpret(
        self,
        base: List[TraceEvent],
        comparison: List[TraceEvent],
        bindings,
        equivalence: Callable,
        evidence_collector: EvidenceCollector,
    ) -> List[EventAlignment]:
        config = self.configuration

        alignments: List[EventAlignment] = []
        used_comparison_indices = set()

        binding_map = {b.base_event_id: b for b in bindings}
        comp_index_by_id = {e.id: idx for idx, e in enumerate(comparison)}

        # Meta-trace emission, keyed by execution sequence numbers.
        meta_run_id = uuid.uuid4()
        meta_seq = [0]

        def emit_meta(payload: AlignmentMetaEvent) -> None:
            if self.meta_trace_callback is None:
                return
            event = TraceEvent(
                run_id=meta_run_id,
                context_id="alignmentEngine",
                engine_name="TraceAlignmentEngine",
                schema_version=1,
                sequence=meta_seq[0],
                span_id=None,
                parent_span_id=None,
                payload=payload,
            )
            meta_seq[0] += 1
            self.meta_trace_callback(event)

        # Relative-order reorder detection: a matched event is reordered only if it forms
        # an inversion in the matched-pair ordering.
        matched_pairs = []  # (base_idx, comp_idx, base_id)
        for i, b_event in enumerate(base):
            binding = binding_map.get(b_event.id)
            if binding is not None and binding.comparison_event_id in comp_index_by_id:
                matched_pairs.append((i, comp_index_by_id[binding.comparison_event_id], b_event.id))

        reordered_base_ids = set()
        for a in matched_pairs:
            for b in matched_pairs:
                if a[2] == b[2]:
                    continue
                if a[0] < b[0] and a[1] > b[1]:
                    reordered_base_ids.add(a[2])
                    reordered_base_ids.add(b[2])

        bound_comparison_indices = {p[1] for p in matched_pairs}

        for i, b_event in enumerate(base):
            ambiguous_options: List[AmbiguousMatch] = []

            binding = binding_map.get(b_event.id)
            match_idx = comp_index_by_id.get(binding.comparison_event_id) if binding else None

            if binding is not None and match_idx is not None:
                c_event = comparison[match_idx]
                score = binding.similarity_score
                # Re-run equivalence for its side effect (records match evidence).
                equivalence(b_event, c_event)
                _, explanation = config.score_match(b_event, c_event)

                emit_meta(
                    AlignmentMetaEvent.evaluated_pair(
                        causal_parent_id=None,
                        decision_node_id=str(uuid.uuid4()),
                        base_sequence=b_event.sequence,
                        comp_sequence=c_event.sequence,
                        score=score,
                    )
                )

                ambiguity_threshold = config.equivalence_evaluator.ambiguity_threshold(b_event.payload)
                best_explanation = explanation

                for j, comp_event in enumerate(comparison):
                    if j == match_idx:
                        continue
                    if j in used_comparison_indices:
                        continue
                    decision_j = equivalence(b_event, comp_event)
                    j_score = decision_j.confidence
                    _, j_explanation = config.score_match(b_event, comp_event)
                    if j_score >= ambiguity_threshold and j not in bound_comparison_indices:
                        delta = score - j_score
                        if delta <= config.profile.ambiguity_delta_threshold:
                            ambiguous_options.append(
                                AmbiguousMatch(event=comp_event, strength=j_score, explanation=j_explanation)
                            )
                            emit_meta(
                                AlignmentMetaEvent.ambiguity_threshold_met(
                                    causal_parent_id=None,
                                    decision_node_id=str(uuid.uuid4()),
                                    comp_sequence=comp_event.sequence,
                                    score=j_score,
                                )
                            )

                ambiguous_options = AlignmentExecutionContract.canonical_sort_ambiguity(ambiguous_options)

                if score < config.profile.semantic_threshold or ambiguous_options:
                    ambiguous_options.append(
                        AmbiguousMatch(event=c_event, strength=score, explanation=best_explanation)
                    )
                    ambiguous_options = AlignmentExecutionContract.canonical_sort_ambiguity(ambiguous_options)
                    if len(ambiguous_options) > config.profile.max_ambiguous_candidates:
                        ambiguous_options = ambiguous_options[: config.profile.max_ambiguous_candidates]
                    state = AlignmentState.ambiguous(len(ambiguous_options))
                    used_comparison_indices.add(match_idx)
                else:
                    used_comparison_indices.add(match_idx)
                    is_reordered = (
                        config.profile.alignment_mode != AlignmentMode.LINEAR
                        and b_event.id in reordered_base_ids
                    )
                    if is_reordered:
                        state = AlignmentState.reordered(b_event.sequence, c_event.sequence)
                    elif b_event.payload == c_event.payload:
                        state = AlignmentState.exact_match()
                    else:
                        state = AlignmentState.semantic_match(score)

                alignments.append(
                    EventAlignment(
                        state=state,
                        base_event=b_event,
                        comparison_event=c_event,
                        explanation=best_explanation,
                        ambiguous_candidates=ambiguous_options,
                    )
                )
                evidence_collector.record_interpretation(
                    InterpretationStep(
                        source_binding=binding,
                        base_id=str(b_event.id),
                        comparison_id=str(c_event.id),
                        output_state=str(state),
                        rationale=best_explanation.primary_reason,
                        base_sequence=b_event.sequence,
                        comparison_sequence=c_event.sequence,
                    )
                )
            else:
                from .alignment_models import AlignmentExplanation

                alignments.append(
                    EventAlignment(
                        state=AlignmentState.removed(),
                        base_event=b_event,
                        comparison_event=None,
                        explanation=AlignmentExplanation.none(),
                    )
                )
                evidence_collector.record_interpretation(
                    InterpretationStep(
                        source_binding=None,
                        base_id=str(b_event.id),
                        comparison_id=None,
                        output_state="removed",
                        rationale="No matching candidate found above threshold.",
                        base_sequence=b_event.sequence,
                        comparison_sequence=None,
                    )
                )

        from .alignment_models import AlignmentExplanation

        for j, c_event in enumerate(comparison):
            if j not in used_comparison_indices:
                alignments.append(
                    EventAlignment(
                        state=AlignmentState.added(),
                        base_event=None,
                        comparison_event=c_event,
                        explanation=AlignmentExplanation.none(),
                    )
                )
                evidence_collector.record_interpretation(
                    InterpretationStep(
                        source_binding=None,
                        base_id=None,
                        comparison_id=str(c_event.id),
                        output_state="added",
                        rationale="Candidate event unassigned to any base event.",
                        base_sequence=None,
                        comparison_sequence=c_event.sequence,
                    )
                )

        return AlignmentExecutionContract.canonical_sort_alignments(alignments)

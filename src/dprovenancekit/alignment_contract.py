"""Canonical ordering, normalization, and the profile hash — a frozen execution spec.

Strips out non-deterministic ordering flicker so two runs of the engine over the same
inputs produce byte-identical render output (and therefore the same snapshot hash).
"""

from __future__ import annotations

import hashlib
from typing import List

from .alignment_models import (
    AmbiguousMatch,
    EventAlignment,
    HeuristicEvidence,
    sort_evidence,
)

CONTRACT_VERSION = "1.0.0"


class AlignmentExecutionContract:
    contract_version = CONTRACT_VERSION

    @staticmethod
    def canonical_sort_evidence(evidence: List[HeuristicEvidence]) -> List[HeuristicEvidence]:
        return sort_evidence(evidence)

    @staticmethod
    def canonical_sort_ambiguity(ambiguity: List[AmbiguousMatch]) -> List[AmbiguousMatch]:
        return sorted(ambiguity, key=lambda a: (-a.strength, a.event.sequence))

    @staticmethod
    def canonical_sort_alignments(alignments: List[EventAlignment]) -> List[EventAlignment]:
        def key(a: EventAlignment):
            base = a.base_event
            comp = a.comparison_event
            seq = (base.sequence if base else (comp.sequence if comp else 0))
            id_ = ""
            if base is not None:
                id_ = str(base.id)
            elif comp is not None:
                id_ = str(comp.id)
            return (seq, id_)

        return sorted(alignments, key=key)

    @staticmethod
    def compute_profile_hash(profile, evaluator_identifier: str, engine_version: str) -> str:
        payload = (
            f"contractVersion:{CONTRACT_VERSION}\n"
            f"engineVersion:{engine_version}\n"
            f"strategy:{profile.strategy.value}\n"
            f"profileVersion:{profile.version}\n"
            f"typeWeight:{_fmt(profile.type_weight)}\n"
            f"payloadWeight:{_fmt(profile.payload_weight)}\n"
            f"structuralWeight:{_fmt(profile.structural_weight)}\n"
            f"temporalWeight:{_fmt(profile.temporal_weight)}\n"
            f"semanticThreshold:{_fmt(profile.semantic_threshold)}\n"
            f"maxAmbiguousCandidates:{profile.max_ambiguous_candidates}\n"
            f"ambiguityDeltaThreshold:{_fmt(profile.ambiguity_delta_threshold)}\n"
            f"alignmentMode:{profile.alignment_mode.value}\n"
            f"evaluatorIdentifier:{evaluator_identifier}"
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _fmt(value: float) -> str:
    """Format a float the way Swift's default ``Double`` interpolation would: an integral
    value keeps one decimal (``0.0``), others use the shortest round-trip repr."""
    if value == int(value):
        return f"{value:.1f}"
    return repr(value)

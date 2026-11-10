"""Experimental, versioned structural trace alignment (v1).

This engine is deliberately separate from TraceAlignmentEngine. It implements the
language-neutral contract in conformance/STRUCTURAL_ALIGNMENT_v1.md and does not
change legacy alignment behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union


class StructuralAlignmentInputError(ValueError):
    """Raised when a structural trace is malformed or configuration is invalid."""


@dataclass(frozen=True)
class StructuralAlignmentConfig:
    type_weight: float = 0.5
    hierarchy_weight: float = 0.3
    payload_weight: float = 0.2
    min_node_conformance: float = 0.8
    min_global_f1_floor: float = 0.95

    def __post_init__(self) -> None:
        weights = (
            self.type_weight,
            self.hierarchy_weight,
            self.payload_weight,
        )
        if any(not 0.0 <= weight <= 1.0 for weight in weights):
            raise StructuralAlignmentInputError("component weights must be in [0, 1]")
        if sum(Decimal(str(weight)) for weight in weights) != Decimal("1"):
            raise StructuralAlignmentInputError("component weights must sum to 1")
        if not 0.0 <= self.min_node_conformance <= 1.0:
            raise StructuralAlignmentInputError("min_node_conformance must be in [0, 1]")
        if not 0.0 <= self.min_global_f1_floor <= 1.0:
            raise StructuralAlignmentInputError("min_global_f1_floor must be in [0, 1]")


@dataclass(frozen=True)
class StructuralAlignmentNode:
    id: str
    parent_id: Optional[str]
    type: str
    payload_signature: str


@dataclass(frozen=True)
class StructuralNodeVerdict:
    target_id: str
    candidate_id: Optional[str]
    suggested_candidate_id: Optional[str]
    type_score: float
    hierarchy_score: float
    payload_score: float
    node_score: float
    matched: bool
    gate_passed: bool
    reason: str


@dataclass(frozen=True)
class StructuralAlignmentResult:
    tp: int
    fp: int
    fn: int
    global_f1: float
    hierarchy_gate_passed: bool
    order_gate_passed: bool
    all_required_nodes_meet_score_threshold: bool
    conforming: bool
    verdicts: Tuple[StructuralNodeVerdict, ...]


@dataclass(frozen=True)
class _ScoredPair:
    target_index: int
    candidate_index: int
    type_score: int
    hierarchy_score: int
    payload_score: int
    score: Decimal


class StructuralAlignmentEngineV1:
    """Deterministic parent-first greedy matcher for ordered event-node traces."""

    VERSION = "structural-alignment-v1"

    def __init__(self, config: Optional[StructuralAlignmentConfig] = None) -> None:
        self.config = config or StructuralAlignmentConfig()
        self._type_weight = Decimal(str(self.config.type_weight))
        self._hierarchy_weight = Decimal(str(self.config.hierarchy_weight))
        self._payload_weight = Decimal(str(self.config.payload_weight))
        self._threshold = Decimal(str(self.config.min_node_conformance))

    @staticmethod
    def _coerce_nodes(
        events: Iterable[Union[StructuralAlignmentNode, Mapping[str, Any]]],
        side: str,
    ) -> List[StructuralAlignmentNode]:
        nodes: List[StructuralAlignmentNode] = []
        for index, event in enumerate(events):
            if isinstance(event, StructuralAlignmentNode):
                node = event
            elif isinstance(event, Mapping):
                try:
                    node = StructuralAlignmentNode(
                        id=event["id"],
                        parent_id=event.get("parent_id"),
                        type=event["type"],
                        payload_signature=event["payload_signature"],
                    )
                except (KeyError, TypeError) as exc:
                    raise StructuralAlignmentInputError(
                        f"{side}[{index}] must contain id, type, and payload_signature"
                    ) from exc
            else:
                raise StructuralAlignmentInputError(
                    f"{side}[{index}] must be a node or mapping"
                )
            if not isinstance(node.id, str) or not node.id:
                raise StructuralAlignmentInputError(f"{side}[{index}] has invalid id")
            if node.parent_id is not None and (
                not isinstance(node.parent_id, str) or not node.parent_id
            ):
                raise StructuralAlignmentInputError(
                    f"{side}[{index}] has invalid parent_id"
                )
            if not isinstance(node.type, str) or not isinstance(
                node.payload_signature, str
            ):
                raise StructuralAlignmentInputError(
                    f"{side}[{index}] type and payload_signature must be strings"
                )
            nodes.append(node)
        return nodes

    @staticmethod
    def _depths(nodes: Sequence[StructuralAlignmentNode], side: str) -> List[int]:
        index_by_id = {node.id: i for i, node in enumerate(nodes)}
        if len(index_by_id) != len(nodes):
            raise StructuralAlignmentInputError(f"{side} contains duplicate node IDs")
        for node in nodes:
            if node.parent_id is not None and node.parent_id not in index_by_id:
                raise StructuralAlignmentInputError(
                    f"{side} node {node.id!r} references missing parent {node.parent_id!r}"
                )

        depths: List[Optional[int]] = [None] * len(nodes)
        visiting: set[str] = set()

        def depth(index: int) -> int:
            known = depths[index]
            if known is not None:
                return known
            node = nodes[index]
            if node.id in visiting:
                raise StructuralAlignmentInputError(f"{side} parent graph contains a cycle")
            visiting.add(node.id)
            if node.parent_id is None:
                result = 0
            else:
                result = depth(index_by_id[node.parent_id]) + 1
            visiting.remove(node.id)
            depths[index] = result
            return result

        for i in range(len(nodes)):
            depth(i)
        return [value if value is not None else 0 for value in depths]

    def _score_pair(
        self,
        target: StructuralAlignmentNode,
        candidate: StructuralAlignmentNode,
        mapped_target_ids: Mapping[str, str],
    ) -> _ScoredPair:
        type_score = int(target.type == candidate.type)
        payload_score = int(target.payload_signature == candidate.payload_signature)
        if target.parent_id is None:
            hierarchy_score = int(candidate.parent_id is None)
        else:
            mapped_parent = mapped_target_ids.get(target.parent_id)
            hierarchy_score = int(
                mapped_parent is not None and candidate.parent_id == mapped_parent
            )
        score = (
            self._type_weight * type_score
            + self._hierarchy_weight * hierarchy_score
            + self._payload_weight * payload_score
        )
        return _ScoredPair(
            target_index=-1,
            candidate_index=-1,
            type_score=type_score,
            hierarchy_score=hierarchy_score,
            payload_score=payload_score,
            score=score,
        )

    def align(
        self,
        target_events: Iterable[Union[StructuralAlignmentNode, Mapping[str, Any]]],
        candidate_events: Iterable[StructuralAlignmentNode | Mapping[str, Any]],
    ) -> StructuralAlignmentResult:
        targets = self._coerce_nodes(target_events, "target")
        candidates = self._coerce_nodes(candidate_events, "candidate")
        target_depths = self._depths(targets, "target")
        self._depths(candidates, "candidate")

        mapped_target_to_candidate: Dict[str, str] = {}
        mapped_pairs: Dict[int, _ScoredPair] = {}
        assigned_candidates: set[int] = set()

        depths_in_order = sorted(set(target_depths))
        for current_depth in depths_in_order:
            depth_targets = [
                i for i, depth_value in enumerate(target_depths)
                if depth_value == current_depth
            ]
            depth_targets.sort(key=lambda i: (i, targets[i].id))
            available_candidates = [
                i for i in range(len(candidates)) if i not in assigned_candidates
            ]
            eligible: List[Tuple[Decimal, int, str, int, str, _ScoredPair]] = []
            for ti in depth_targets:
                for ci in available_candidates:
                    base = self._score_pair(
                        targets[ti], candidates[ci], mapped_target_to_candidate
                    )
                    pair = _ScoredPair(
                        target_index=ti,
                        candidate_index=ci,
                        type_score=base.type_score,
                        hierarchy_score=base.hierarchy_score,
                        payload_score=base.payload_score,
                        score=base.score,
                    )
                    if pair.score >= self._threshold:
                        eligible.append(
                            (
                                -pair.score,
                                ci,
                                candidates[ci].id,
                                ti,
                                targets[ti].id,
                                pair,
                            )
                        )
            eligible.sort(key=lambda item: item[:5])
            assigned_targets: set[int] = set()
            for _, ci, _, ti, _, pair in eligible:
                if ti in assigned_targets or ci in assigned_candidates:
                    continue
                assigned_targets.add(ti)
                assigned_candidates.add(ci)
                mapped_target_to_candidate[targets[ti].id] = candidates[ci].id
                mapped_pairs[ti] = pair

        # Suggestions are diagnostic only. They are chosen after all eligible mappings,
        # from candidates not consumed by a mapping, and require a strictly positive score.
        suggestions: Dict[int, Tuple[int, _ScoredPair]] = {}
        for ti, target in enumerate(targets):
            if ti in mapped_pairs:
                continue
            remaining = [
                ci for ci in range(len(candidates)) if ci not in assigned_candidates
            ]
            ranked: List[Tuple[Decimal, int, str, _ScoredPair]] = []
            for ci in remaining:
                scored = self._score_pair(
                    target, candidates[ci], mapped_target_to_candidate
                )
                if scored.score > 0:
                    pair = _ScoredPair(
                        target_index=ti,
                        candidate_index=ci,
                        type_score=scored.type_score,
                        hierarchy_score=scored.hierarchy_score,
                        payload_score=scored.payload_score,
                        score=scored.score,
                    )
                    ranked.append(
                        (-pair.score, ci, candidates[ci].id, pair)
                    )
            if ranked:
                ranked.sort(key=lambda item: item[:3])
                chosen = ranked[0][3]
                suggestions[ti] = (chosen.candidate_index, chosen)

        # Detect every mapped node participating in an order inversion.
        reordered: set[int] = set()
        matched_indices = sorted(mapped_pairs)
        for offset, left_ti in enumerate(matched_indices):
            left_ci = mapped_pairs[left_ti].candidate_index
            for right_ti in matched_indices[offset + 1:]:
                right_ci = mapped_pairs[right_ti].candidate_index
                if left_ci > right_ci:
                    reordered.add(left_ti)
                    reordered.add(right_ti)

        verdicts: List[StructuralNodeVerdict] = []
        for ti, target in enumerate(targets):
            pair = mapped_pairs.get(ti)
            suggestion = suggestions.get(ti)
            if pair is not None:
                candidate_id: Optional[str] = candidates[pair.candidate_index].id
                suggested_id = None
                type_score = pair.type_score
                hierarchy_score = pair.hierarchy_score
                payload_score = pair.payload_score
                node_score = float(pair.score)
                matched = True
                if hierarchy_score == 0:
                    gate_passed = False
                    reason = "PARENT_HIERARCHY_MISMATCH"
                elif ti in reordered:
                    gate_passed = False
                    reason = "REORDERED_REQUIRED_EVENTS"
                else:
                    gate_passed = True
                    reason = "CONFORMANT"
            elif suggestion is not None:
                suggested_pair = suggestion[1]
                candidate_id = None
                suggested_id = candidates[suggested_pair.candidate_index].id
                type_score = suggested_pair.type_score
                hierarchy_score = suggested_pair.hierarchy_score
                payload_score = suggested_pair.payload_score
                node_score = float(suggested_pair.score)
                matched = False
                gate_passed = False
                if hierarchy_score == 0:
                    reason = "PARENT_HIERARCHY_MISMATCH"
                else:
                    reason = "NODE_SCORE_BELOW_THRESHOLD"
            else:
                candidate_id = None
                suggested_id = None
                type_score = hierarchy_score = payload_score = 0
                node_score = 0.0
                matched = False
                gate_passed = False
                reason = "MISSING_REQUIRED_EVENT"

            verdicts.append(
                StructuralNodeVerdict(
                    target_id=target.id,
                    candidate_id=candidate_id,
                    suggested_candidate_id=suggested_id,
                    type_score=float(type_score),
                    hierarchy_score=float(hierarchy_score),
                    payload_score=float(payload_score),
                    node_score=node_score,
                    matched=matched,
                    gate_passed=gate_passed,
                    reason=reason,
                )
            )

        tp = len(mapped_pairs)
        fn = len(targets) - tp
        fp = len(candidates) - len(assigned_candidates)
        denominator = 2 * tp + fp + fn
        global_f1 = (2 * tp / denominator) if denominator else 0.0
        hierarchy_gate_passed = all(
            pair.hierarchy_score == 1 for pair in mapped_pairs.values()
        )
        order_gate_passed = not reordered
        all_required_nodes_meet_score_threshold = len(mapped_pairs) == len(targets)
        conforming = (
            all_required_nodes_meet_score_threshold
            and global_f1 >= self.config.min_global_f1_floor
            and hierarchy_gate_passed
            and order_gate_passed
        )
        return StructuralAlignmentResult(
            tp=tp,
            fp=fp,
            fn=fn,
            global_f1=global_f1,
            hierarchy_gate_passed=hierarchy_gate_passed,
            order_gate_passed=order_gate_passed,
            all_required_nodes_meet_score_threshold=(
                all_required_nodes_meet_score_threshold
            ),
            conforming=conforming,
            verdicts=tuple(verdicts),
        )

"""Globally optimal bipartite assignment using the production score function.

Implements the Hungarian / Munkres algorithm in pure Python (no scipy required).
Maximizes total match score over base↔comparison pairs that clear the same
ambiguity threshold the production ``DefaultTraceMatcher`` uses.
"""

from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

from dprovenancekit.alignment_evidence import AlignmentBinding


def hungarian_maximize(score_matrix: List[List[float]]) -> List[Tuple[int, int]]:
    """Return row→col assignments maximizing sum of scores.

    ``score_matrix[i][j]`` is the benefit of assigning row i to column j.
    Incomplete rectangular matrices are padded with zeros. Unassigned rows/cols
    (only zeros) are omitted from the result.
    """
    if not score_matrix or not score_matrix[0]:
        return []

    n_rows = len(score_matrix)
    n_cols = len(score_matrix[0])
    n = max(n_rows, n_cols)

    # Convert maximize → minimize by subtracting from a large constant.
    max_val = max(max(row) for row in score_matrix) if score_matrix else 0.0
    # Cost matrix (n x n), padded with max_val so padded cells are never preferred
    # over real positive scores when maximizing (cost 0 after transform for max cells).
    cost = [[max_val - 0.0 for _ in range(n)] for _ in range(n)]
    for i in range(n_rows):
        for j in range(n_cols):
            cost[i][j] = max_val - score_matrix[i][j]

    # Munkres / Kuhn-Munkres (Jonker-Volgenant style simplified for dense n).
    # Classic O(n^3) implementation adapted from the public-domain Munkres algorithm.
    u = [0.0] * (n + 1)
    v = [0.0] * (n + 1)
    p = [0] * (n + 1)
    way = [0] * (n + 1)

    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = [float("inf")] * (n + 1)
        used = [False] * (n + 1)
        while True:
            used[j0] = True
            i0 = p[j0]
            delta = float("inf")
            j1 = 0
            for j in range(1, n + 1):
                if used[j]:
                    continue
                cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                if cur < minv[j]:
                    minv[j] = cur
                    way[j] = j0
                if minv[j] < delta:
                    delta = minv[j]
                    j1 = j
            for j in range(n + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while True:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
            if j0 == 0:
                break

    # p[j] = row assigned to column j (1-indexed); invert.
    assignment: List[Tuple[int, int]] = []
    for j in range(1, n + 1):
        i = p[j]
        if i == 0:
            continue
        row, col = i - 1, j - 1
        if row < n_rows and col < n_cols and score_matrix[row][col] > 0.0:
            assignment.append((row, col))
    return assignment


def optimal_bindings(
    configuration,
    base: Sequence,
    comparison: Sequence,
) -> List[AlignmentBinding]:
    """Compute globally optimal bindings under production scoring + thresholds.

    Mirrors ``DefaultTraceMatcher`` eligibility (score >= ambiguity_threshold) but
    selects a maximum-weight matching instead of greedy highest-score-first.
    """
    n_b, n_c = len(base), len(comparison)
    if n_b == 0 or n_c == 0:
        return []

    score_matrix: List[List[float]] = [[0.0] * n_c for _ in range(n_b)]
    for i, b_event in enumerate(base):
        threshold = configuration.equivalence_evaluator.ambiguity_threshold(
            b_event.payload
        )
        for j, c_event in enumerate(comparison):
            score, _ = configuration.score_match(b_event, c_event)
            if score >= threshold:
                score_matrix[i][j] = score

    pairs = hungarian_maximize(score_matrix)
    bindings: List[AlignmentBinding] = []
    for i, j in pairs:
        bindings.append(
            AlignmentBinding(
                base_event_id=base[i].id,
                comparison_event_id=comparison[j].id,
                similarity_score=score_matrix[i][j],
            )
        )
    return bindings


def greedy_bindings(configuration, base, comparison) -> List[AlignmentBinding]:
    """Production greedy matcher without evidence side-effects."""
    from dprovenancekit.alignment_evidence import NullEvidenceCollector
    from dprovenancekit.alignment_matcher import DefaultTraceMatcher

    return DefaultTraceMatcher(configuration).match(
        list(base), list(comparison), evidence_collector=NullEvidenceCollector()
    )


def binding_pair_set(bindings: List[AlignmentBinding]) -> set:
    return {(b.base_event_id, b.comparison_event_id) for b in bindings}


def total_score(bindings: List[AlignmentBinding]) -> float:
    return sum(b.similarity_score for b in bindings)

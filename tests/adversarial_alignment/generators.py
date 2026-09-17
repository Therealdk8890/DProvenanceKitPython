"""Pathological trace pair generators for adversarial alignment testing."""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from dprovenancekit import TraceEvent, TracePriority, TraceRun, TraceableEvent


@dataclass(frozen=True)
class AdvEvent(TraceableEvent):
    """Minimal payload used by adversarial generators."""

    kind: str
    body: str = ""
    critical: bool = False
    # Optional hook for semantic-evaluator-disagree cases (ignored by == / hashing).
    semantic_label: str = ""

    @property
    def type_identifier(self) -> str:
        return self.kind

    @property
    def priority(self) -> TracePriority:
        return TracePriority.CRITICAL if self.critical else TracePriority.STRUCTURAL

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "body": self.body,
            "critical": self.critical,
            "semantic_label": self.semantic_label,
        }


@dataclass(frozen=True)
class Case:
    name: str
    category: str
    base: TraceRun
    comparison: TraceRun
    notes: str = ""


def _mk_run(
    specs: Sequence[Tuple[int, AdvEvent]],
    *,
    run_id: Optional[uuid.UUID] = None,
    engine: str = "adv",
    parent_span_id: Optional[str] = None,
) -> TraceRun:
    rid = run_id or uuid.uuid4()
    events = [
        TraceEvent(
            run_id=rid,
            context_id="adv_ctx",
            engine_name=engine,
            schema_version=1,
            sequence=seq,
            span_id=f"span-{seq}",
            parent_span_id=parent_span_id,
            payload=payload,
        )
        for seq, payload in specs
    ]
    return TraceRun(run_id=rid, context_id="adv_ctx", events=events)


# ---------------------------------------------------------------------------
# Original catalog (kept for continuity with v1 metrics)
# ---------------------------------------------------------------------------


def duplicate_event_types() -> Case:
    base = _mk_run(
        [
            (0, AdvEvent("tool_call", "search:q1", critical=True)),
            (1, AdvEvent("tool_call", "search:q2", critical=True)),
            (2, AdvEvent("tool_call", "search:q3", critical=True)),
        ]
    )
    comp = _mk_run(
        [
            (0, AdvEvent("tool_call", "search:q3", critical=True)),
            (1, AdvEvent("tool_call", "search:q1-near", critical=True)),
            (2, AdvEvent("tool_call", "search:q2", critical=True)),
            (3, AdvEvent("tool_call", "search:q1", critical=True)),
        ]
    )
    return Case("duplicate_event_types", "duplicates", base, comp)


def repeated_tool_calls() -> Case:
    base = _mk_run(
        [(i, AdvEvent("tool_call.start", f"tool={i%3}", critical=False)) for i in range(8)]
    )
    specs = []
    seq = 0
    for i in range(8):
        if i in (2, 5):
            specs.append((seq, AdvEvent("tool_call.start", f"decoy={i}", critical=False)))
            seq += 1
        specs.append((seq, AdvEvent("tool_call.start", f"tool={i%3}", critical=False)))
        seq += 1
    comp = _mk_run(specs)
    return Case("repeated_tool_calls", "duplicates", base, comp)


def near_identical_payloads() -> Case:
    base = _mk_run(
        [
            (0, AdvEvent("decision", "authorize:alice:100", critical=True)),
            (1, AdvEvent("decision", "authorize:bob:50", critical=True)),
        ]
    )
    comp = _mk_run(
        [
            (0, AdvEvent("decision", "authorize:alice:100 ", critical=True)),
            (1, AdvEvent("decision", "authorize:bob:50", critical=True)),
            (2, AdvEvent("decision", "authorize:alice:100", critical=True)),
        ]
    )
    return Case("near_identical_payloads", "near_identical", base, comp)


def inserted_decoys() -> Case:
    base = _mk_run(
        [
            (0, AdvEvent("createCustomer", "x", critical=True)),
            (1, AdvEvent("generateInvoice", "y", critical=True)),
        ]
    )
    comp = _mk_run(
        [
            (0, AdvEvent("log", "noise", critical=False)),
            (1, AdvEvent("createCustomer", "x", critical=True)),
            (2, AdvEvent("log", "more-noise", critical=False)),
            (3, AdvEvent("generateInvoice", "y", critical=True)),
            (4, AdvEvent("decision", "decoy", critical=True)),
        ]
    )
    return Case("inserted_decoys", "insert_delete", base, comp)


def deleted_events() -> Case:
    base = _mk_run(
        [
            (0, AdvEvent("createCustomer", "x", critical=True)),
            (1, AdvEvent("validate", "perms", critical=True)),
            (2, AdvEvent("generateInvoice", "y", critical=True)),
        ]
    )
    comp = _mk_run(
        [
            (0, AdvEvent("createCustomer", "x", critical=True)),
            (1, AdvEvent("generateInvoice", "y", critical=True)),
        ]
    )
    return Case("deleted_events", "insert_delete", base, comp)


def reordered_events() -> Case:
    base = _mk_run(
        [
            (0, AdvEvent("createCustomer", "x", critical=True)),
            (1, AdvEvent("generateInvoice", "y", critical=True)),
            (2, AdvEvent("sendEmail", "z", critical=True)),
        ]
    )
    comp = _mk_run(
        [
            (0, AdvEvent("generateInvoice", "y", critical=True)),
            (1, AdvEvent("sendEmail", "z", critical=True)),
            (2, AdvEvent("createCustomer", "x", critical=True)),
        ]
    )
    return Case("reordered_events", "reorder", base, comp)


def equally_scored_candidates() -> Case:
    base = _mk_run([(0, AdvEvent("fetch", "same", critical=False))])
    comp = _mk_run(
        [
            (0, AdvEvent("fetch", "same", critical=False)),
            (1, AdvEvent("fetch", "same", critical=False)),
            (2, AdvEvent("fetch", "same", critical=False)),
        ]
    )
    return Case("equally_scored_candidates", "ties", base, comp)


def one_to_many_collisions() -> Case:
    """Classic greedy trap: early weak match steals column needed by later exact."""
    base = _mk_run(
        [
            (0, AdvEvent("decision", "weak-prefer-A", critical=True)),
            (1, AdvEvent("decision", "exact-B", critical=True)),
        ]
    )
    comp = _mk_run(
        [
            (0, AdvEvent("decision", "exact-B", critical=True)),
            (1, AdvEvent("decision", "weak-prefer-A", critical=True)),
        ]
    )
    return Case("one_to_many_collisions", "collisions", base, comp)


def threshold_boundary_scores() -> List[Case]:
    cases = []
    for label, body_a, body_b in [
        ("below", "payload-A", "payload-A-almost"),
        ("at", "payload-B", "payload-B"),
        ("above", "payload-C", "payload-C"),
    ]:
        base = _mk_run([(0, AdvEvent("step", body_a, critical=True))])
        comp = _mk_run([(0, AdvEvent("step", body_b, critical=True))])
        cases.append(
            Case(f"threshold_boundary_{label}", "threshold", base, comp, notes=label)
        )
    return cases


def critical_structural_mix() -> Case:
    base = _mk_run(
        [
            (0, AdvEvent("log", "l1", critical=False)),
            (1, AdvEvent("authorize", "a", critical=True)),
            (2, AdvEvent("log", "l2", critical=False)),
            (3, AdvEvent("finalize", "f", critical=True)),
        ]
    )
    comp = _mk_run(
        [
            (0, AdvEvent("authorize", "a", critical=True)),
            (1, AdvEvent("log", "l2", critical=False)),
            (2, AdvEvent("finalize", "f", critical=True)),
            (3, AdvEvent("log", "l1", critical=False)),
            (4, AdvEvent("log", "extra", critical=False)),
        ]
    )
    return Case("critical_structural_mix", "priority_mix", base, comp)


def long_repeated_patterns() -> Case:
    return long_repeated_patterns_n(30, name="long_repeated_patterns")


def semantic_evaluator_disagree_hooks() -> Case:
    base = _mk_run(
        [
            (0, AdvEvent("claim", "text-A", critical=True, semantic_label="cluster-1")),
            (1, AdvEvent("claim", "text-B", critical=True, semantic_label="cluster-2")),
        ]
    )
    comp = _mk_run(
        [
            (0, AdvEvent("claim", "text-B-paraphrase", critical=True, semantic_label="cluster-2")),
            (1, AdvEvent("claim", "text-A-paraphrase", critical=True, semantic_label="cluster-1")),
        ]
    )
    return Case(
        "semantic_evaluator_disagree",
        "semantic_hook",
        base,
        comp,
        notes="Requires custom evaluator that scores by semantic_label; skipped if unavailable.",
    )


def greedy_trap_assignment() -> Case:
    """Payloads encode intended grades for GradedTrap_v1 evaluator."""
    base = _mk_run(
        [
            (0, AdvEvent("graded", "b0", critical=True)),
            (1, AdvEvent("graded", "b1", critical=True)),
        ]
    )
    comp = _mk_run(
        [
            (0, AdvEvent("graded", "c0", critical=True)),
            (1, AdvEvent("graded", "c1", critical=True)),
        ]
    )
    return Case("greedy_trap_assignment", "collisions", base, comp, notes="graded")


# ---------------------------------------------------------------------------
# v2 expansions
# ---------------------------------------------------------------------------


def long_repeated_patterns_n(n_events: int, *, name: Optional[str] = None) -> Case:
    """Repeated plan/tool/observe cycles totaling ~n_events (50–200)."""
    pattern = ["plan", "tool_call", "observe", "tool_call", "conclude"]
    cycles = max(1, n_events // len(pattern))
    base_specs = []
    for cycle in range(cycles):
        for i, kind in enumerate(pattern):
            seq = cycle * len(pattern) + i
            crit = kind in ("plan", "conclude")
            base_specs.append((seq, AdvEvent(kind, f"c{cycle}:{kind}", critical=crit)))

    comp_specs = []
    seq = 0
    for cycle in range(cycles):
        kinds = list(pattern)
        if cycle % 7 == 2:
            kinds = ["plan", "tool_call", "tool_call", "observe", "conclude"]
        if cycle % 11 == 4:
            kinds = ["conclude", "plan", "tool_call", "observe", "tool_call"]
        if cycle % 13 == 3:
            kinds = ["plan", "tool_call", "observe", "tool_call"]
        for kind in kinds:
            crit = kind in ("plan", "conclude")
            body = f"c{cycle}:{kind}"
            if cycle % 7 == 2 and kind == "tool_call" and seq % 2 == 0:
                body = f"c{cycle}:decoy"
            comp_specs.append((seq, AdvEvent(kind, body, critical=crit)))
            seq += 1

    case_name = name or f"long_repeated_{n_events}"
    return Case(
        case_name,
        "long",
        _mk_run(base_specs),
        _mk_run(comp_specs),
        notes=f"n≈{len(base_specs)}",
    )


def nested_interleaved_duplicates() -> List[Case]:
    """Nested same-type blocks interleaved with decoys and cross-block swaps."""
    cases: List[Case] = []

    # Nested: outer block of decisions wrapping an inner identical-type burst.
    base = _mk_run(
        [
            (0, AdvEvent("decision", "outer-A", critical=True)),
            (1, AdvEvent("decision", "inner-1", critical=True)),
            (2, AdvEvent("decision", "inner-2", critical=True)),
            (3, AdvEvent("decision", "outer-B", critical=True)),
        ]
    )
    comp = _mk_run(
        [
            (0, AdvEvent("decision", "outer-B", critical=True)),
            (1, AdvEvent("log", "noise", critical=False)),
            (2, AdvEvent("decision", "inner-2", critical=True)),
            (3, AdvEvent("decision", "inner-1", critical=True)),
            (4, AdvEvent("decision", "outer-A", critical=True)),
            (5, AdvEvent("decision", "inner-decoy", critical=True)),
        ]
    )
    cases.append(Case("nested_duplicates_swap", "duplicates", base, comp))

    # Interleaved A/B streams with duplicate bodies across streams.
    base2 = _mk_run(
        [
            (i, AdvEvent("stream", f"{'A' if i % 2 == 0 else 'B'}:{i // 2}", critical=i % 2 == 0))
            for i in range(12)
        ]
    )
    # Reverse B stream while keeping A; insert decoy B.
    comp2_specs = []
    seq = 0
    a_vals = [f"A:{i}" for i in range(6)]
    b_vals = [f"B:{i}" for i in range(6)][::-1]
    ai = bi = 0
    for i in range(14):
        if i == 5:
            comp2_specs.append((seq, AdvEvent("stream", "B:decoy", critical=False)))
            seq += 1
            continue
        if i % 2 == 0 and ai < len(a_vals):
            comp2_specs.append((seq, AdvEvent("stream", a_vals[ai], critical=True)))
            ai += 1
        elif bi < len(b_vals):
            comp2_specs.append((seq, AdvEvent("stream", b_vals[bi], critical=False)))
            bi += 1
        seq += 1
    cases.append(
        Case(
            "interleaved_ab_duplicates",
            "duplicates",
            base2,
            _mk_run(comp2_specs),
        )
    )

    # Triple-nested same payload at different depths (equal scores).
    base3 = _mk_run([(i, AdvEvent("nest", "same", critical=True)) for i in range(6)])
    comp3 = _mk_run(
        [(i, AdvEvent("nest", "same", critical=True)) for i in range(9)]
        + [(9, AdvEvent("nest", "near", critical=True))]
    )
    cases.append(Case("nested_equal_payload_burst", "ties", base3, comp3))
    return cases


def many_equally_scored_candidates() -> List[Case]:
    cases: List[Case] = []
    for n_base, n_comp in [(1, 8), (3, 9), (5, 5), (8, 12)]:
        base = _mk_run(
            [(i, AdvEvent("fetch", "same", critical=False)) for i in range(n_base)]
        )
        comp = _mk_run(
            [(i, AdvEvent("fetch", "same", critical=False)) for i in range(n_comp)]
        )
        cases.append(
            Case(
                f"ties_{n_base}x{n_comp}",
                "ties",
                base,
                comp,
                notes="all equal scores",
            )
        )
    # Mixed: half exact, half equal-tie decoys of same type.
    base = _mk_run(
        [(i, AdvEvent("item", f"exact-{i}", critical=True)) for i in range(4)]
    )
    comp_specs = [(i, AdvEvent("item", "tie", critical=True)) for i in range(6)]
    for i in range(4):
        comp_specs.append((6 + i, AdvEvent("item", f"exact-{i}", critical=True)))
    cases.append(
        Case("ties_with_exact_anchors", "ties", base, _mk_run(comp_specs))
    )
    return cases


def one_to_many_collision_family() -> List[Case]:
    cases: List[Case] = [one_to_many_collisions()]

    # 3×3 permutation with one exact diagonal and near-miss off-diagonals.
    bodies_b = ["alpha", "beta", "gamma"]
    bodies_c = ["gamma", "alpha", "beta"]  # rotation
    base = _mk_run(
        [(i, AdvEvent("decision", bodies_b[i], critical=True)) for i in range(3)]
    )
    comp = _mk_run(
        [(i, AdvEvent("decision", bodies_c[i], critical=True)) for i in range(3)]
    )
    cases.append(Case("collision_rotate_3", "collisions", base, comp))

    # Many-to-one: several base events compete for one exact column.
    base = _mk_run(
        [
            (0, AdvEvent("decision", "target", critical=True)),
            (1, AdvEvent("decision", "target-near", critical=True)),
            (2, AdvEvent("decision", "other", critical=True)),
        ]
    )
    comp = _mk_run(
        [
            (0, AdvEvent("decision", "target", critical=True)),
            (1, AdvEvent("decision", "other", critical=True)),
            (2, AdvEvent("decision", "spare", critical=True)),
        ]
    )
    cases.append(Case("collision_many_to_one", "collisions", base, comp))

    # Chain: each base prefers next column weakly, last prefers first exactly.
    n = 5
    base = _mk_run(
        [(i, AdvEvent("chain", f"b{i}", critical=True)) for i in range(n)]
    )
    # Comparison has exact matches rotated by 1.
    comp = _mk_run(
        [(i, AdvEvent("chain", f"b{(i + 1) % n}", critical=True)) for i in range(n)]
    )
    cases.append(Case("collision_chain_rotate_5", "collisions", base, comp))
    return cases


def greedy_trap_family() -> List[Case]:
    """Multiple asymmetric graded traps (payloads encode grades for GradedTrap_*)."""
    cases = [greedy_trap_assignment()]

    # 3-row trap: greedy takes local maxes; optimal reassigns.
    # Bodies: b{i} / c{j}; graded evaluator maps known pairs.
    base = _mk_run(
        [(i, AdvEvent("graded", f"t3_b{i}", critical=True)) for i in range(3)]
    )
    comp = _mk_run(
        [(j, AdvEvent("graded", f"t3_c{j}", critical=True)) for j in range(3)]
    )
    cases.append(Case("greedy_trap_3x3", "collisions", base, comp, notes="graded_3x3"))

    # 4-row sparse trap.
    base = _mk_run(
        [(i, AdvEvent("graded", f"t4_b{i}", critical=True)) for i in range(4)]
    )
    comp = _mk_run(
        [(j, AdvEvent("graded", f"t4_c{j}", critical=True)) for j in range(4)]
    )
    cases.append(Case("greedy_trap_4x4", "collisions", base, comp, notes="graded_4x4"))

    # Trap where greedy leaves a critical unbound that optimal would bind.
    base = _mk_run(
        [
            (0, AdvEvent("graded", "steal_b0", critical=True)),
            (1, AdvEvent("graded", "steal_b1", critical=True)),
        ]
    )
    comp = _mk_run(
        [
            (0, AdvEvent("graded", "steal_c0", critical=True)),
            (1, AdvEvent("graded", "steal_c1", critical=True)),
        ]
    )
    cases.append(Case("greedy_trap_steal_column", "collisions", base, comp, notes="graded_steal"))
    return cases


def threshold_boundary_expanded() -> List[Case]:
    """Below / at / above semantic_threshold plus bind-floor neighborhood."""
    cases = threshold_boundary_scores()
    # Extra critical pairs around strict_audit 0.99 and developer_debug 0.75.
    for label, body_a, body_b in [
        ("near_miss_space", "exact-body", "exact-body "),
        ("near_miss_case", "Exact", "exact"),
        ("identical_pair", "same", "same"),
        ("total_mismatch", "alpha", "omega"),
    ]:
        base = _mk_run([(0, AdvEvent("step", body_a, critical=True))])
        comp = _mk_run([(0, AdvEvent("step", body_b, critical=True))])
        cases.append(Case(f"threshold_extra_{label}", "threshold", base, comp, notes=label))

    # Multi-event threshold: one below, one above in same run.
    base = _mk_run(
        [
            (0, AdvEvent("step", "keep", critical=True)),
            (1, AdvEvent("step", "change-me", critical=True)),
        ]
    )
    comp = _mk_run(
        [
            (0, AdvEvent("step", "keep", critical=True)),
            (1, AdvEvent("step", "change-me-almost", critical=True)),
        ]
    )
    cases.append(Case("threshold_mixed_pair", "threshold", base, comp))
    return cases


def critical_structural_priority_interactions() -> List[Case]:
    cases = [critical_structural_mix()]

    # Structural decoys between criticals that reorder structurally only.
    base = _mk_run(
        [
            (0, AdvEvent("authorize", "a", critical=True)),
            (1, AdvEvent("log", "l1", critical=False)),
            (2, AdvEvent("finalize", "f", critical=True)),
            (3, AdvEvent("log", "l2", critical=False)),
        ]
    )
    comp = _mk_run(
        [
            (0, AdvEvent("log", "l2", critical=False)),
            (1, AdvEvent("authorize", "a", critical=True)),
            (2, AdvEvent("log", "l1", critical=False)),
            (3, AdvEvent("finalize", "f", critical=True)),
            (4, AdvEvent("log", "extra", critical=False)),
        ]
    )
    cases.append(Case("priority_structural_shuffle_criticals_stable", "priority_mix", base, comp))

    # Critical reorder amid structural noise.
    base = _mk_run(
        [
            (0, AdvEvent("log", "n0", critical=False)),
            (1, AdvEvent("createCustomer", "x", critical=True)),
            (2, AdvEvent("log", "n1", critical=False)),
            (3, AdvEvent("generateInvoice", "y", critical=True)),
            (4, AdvEvent("log", "n2", critical=False)),
        ]
    )
    comp = _mk_run(
        [
            (0, AdvEvent("log", "n2", critical=False)),
            (1, AdvEvent("generateInvoice", "y", critical=True)),
            (2, AdvEvent("log", "n0", critical=False)),
            (3, AdvEvent("createCustomer", "x", critical=True)),
            (4, AdvEvent("log", "n1", critical=False)),
            (5, AdvEvent("log", "n3", critical=False)),
        ]
    )
    cases.append(Case("priority_critical_reorder_with_noise", "priority_mix", base, comp))

    # Critical deleted, structural fills gap with same type identifier.
    base = _mk_run(
        [
            (0, AdvEvent("decision", "validate", critical=True)),
            (1, AdvEvent("decision", "charge", critical=True)),
        ]
    )
    comp = _mk_run(
        [
            (0, AdvEvent("decision", "telemetry-lookalike", critical=False)),
            (1, AdvEvent("decision", "charge", critical=True)),
        ]
    )
    cases.append(Case("priority_critical_replaced_by_structural", "priority_mix", base, comp))

    # All-structural reorder (should stay none under ExactEquality criticals).
    base = _mk_run(
        [(i, AdvEvent("log", f"l{i}", critical=False)) for i in range(6)]
    )
    comp = _mk_run(
        [(i, AdvEvent("log", f"l{5 - i}", critical=False)) for i in range(6)]
    )
    cases.append(Case("priority_all_structural_reorder", "priority_mix", base, comp))
    return cases


def semantic_hook_family() -> List[Case]:
    cases = [semantic_evaluator_disagree_hooks()]
    # Three clusters crossed.
    base = _mk_run(
        [
            (0, AdvEvent("claim", "t0", critical=True, semantic_label="c0")),
            (1, AdvEvent("claim", "t1", critical=True, semantic_label="c1")),
            (2, AdvEvent("claim", "t2", critical=True, semantic_label="c2")),
        ]
    )
    comp = _mk_run(
        [
            (0, AdvEvent("claim", "p2", critical=True, semantic_label="c2")),
            (1, AdvEvent("claim", "p0", critical=True, semantic_label="c0")),
            (2, AdvEvent("claim", "p1", critical=True, semantic_label="c1")),
        ]
    )
    cases.append(
        Case(
            "semantic_three_cluster_cross",
            "semantic_hook",
            base,
            comp,
            notes="SemanticLabel_v1",
        )
    )
    # Partial: one label match, one payload-only mismatch.
    base = _mk_run(
        [
            (0, AdvEvent("claim", "keep", critical=True, semantic_label="same")),
            (1, AdvEvent("claim", "drift", critical=True, semantic_label="x")),
        ]
    )
    comp = _mk_run(
        [
            (0, AdvEvent("claim", "keep-para", critical=True, semantic_label="same")),
            (1, AdvEvent("claim", "other", critical=True, semantic_label="y")),
        ]
    )
    cases.append(
        Case(
            "semantic_partial_cluster",
            "semantic_hook",
            base,
            comp,
            notes="SemanticLabel_v1",
        )
    )
    return cases


def seeded_fuzz_families(*, seeds: Sequence[int] = (1, 2, 3, 7, 11, 42, 99, 123)) -> List[Case]:
    """Deterministic random pathological families (fixed seeds)."""
    cases: List[Case] = []
    kinds = ["plan", "tool_call", "observe", "decision", "log", "finalize"]
    for seed in seeds:
        rng = random.Random(seed)
        n = rng.randint(50, 200)
        base_specs = []
        for i in range(n):
            kind = kinds[rng.randrange(len(kinds))]
            crit = kind in ("plan", "decision", "finalize")
            body = f"s{seed}:{kind}:{rng.randint(0, 5)}"
            base_specs.append((i, AdvEvent(kind, body, critical=crit)))

        # Mutate: shuffle a window, delete some, insert decoys, near-miss rewrite.
        bodies = [p.body for _, p in base_specs]
        kinds_b = [p.kind for _, p in base_specs]
        crits = [p.critical for _, p in base_specs]

        # Window shuffle
        if n >= 10:
            lo = rng.randint(0, n - 10)
            hi = lo + rng.randint(4, 10)
            window = list(range(lo, hi))
            rng.shuffle(window)
            new_order = list(range(n))
            for idx, src in enumerate(window):
                new_order[lo + idx] = src
        else:
            new_order = list(range(n))

        comp_specs = []
        seq = 0
        deleted = set(rng.sample(range(n), k=min(n // 10, 8))) if n >= 10 else set()
        for src in new_order:
            if src in deleted:
                continue
            kind = kinds_b[src]
            body = bodies[src]
            crit = crits[src]
            if rng.random() < 0.08:
                body = body + "-near"
            comp_specs.append((seq, AdvEvent(kind, body, critical=crit)))
            seq += 1
            if rng.random() < 0.06:
                decoy_kind = kinds[rng.randrange(len(kinds))]
                comp_specs.append(
                    (
                        seq,
                        AdvEvent(
                            decoy_kind,
                            f"decoy-{seed}-{seq}",
                            critical=decoy_kind in ("plan", "decision"),
                        ),
                    )
                )
                seq += 1

        cases.append(
            Case(
                f"fuzz_seed_{seed}_n{n}",
                "fuzz",
                _mk_run(base_specs),
                _mk_run(comp_specs),
                notes=f"seed={seed}",
            )
        )
    return cases


def near_identical_family() -> List[Case]:
    cases = [near_identical_payloads()]
    for suffix, a, b in [
        ("trailing_tab", "val", "val\t"),
        ("double_space", "a b", "a  b"),
        ("prefix", "id:42", "xid:42"),
    ]:
        base = _mk_run([(0, AdvEvent("decision", a, critical=True))])
        comp = _mk_run(
            [
                (0, AdvEvent("decision", b, critical=True)),
                (1, AdvEvent("decision", a, critical=True)),
            ]
        )
        cases.append(Case(f"near_identical_{suffix}", "near_identical", base, comp))
    return cases


def insert_delete_family() -> List[Case]:
    cases = [inserted_decoys(), deleted_events()]
    # Bulk inserts
    base = _mk_run(
        [(i, AdvEvent("step", f"s{i}", critical=True)) for i in range(5)]
    )
    comp_specs = []
    seq = 0
    for i in range(5):
        for _ in range(3):
            comp_specs.append((seq, AdvEvent("noise", f"n{seq}", critical=False)))
            seq += 1
        comp_specs.append((seq, AdvEvent("step", f"s{i}", critical=True)))
        seq += 1
    cases.append(Case("insert_bulk_noise", "insert_delete", base, _mk_run(comp_specs)))

    # Bulk deletes of every other critical
    base = _mk_run(
        [(i, AdvEvent("step", f"s{i}", critical=True)) for i in range(8)]
    )
    comp = _mk_run(
        [(i, AdvEvent("step", f"s{i * 2}", critical=True)) for i in range(4)]
    )
    cases.append(Case("delete_every_other", "insert_delete", base, comp))
    return cases


def reorder_family() -> List[Case]:
    cases = [reordered_events()]
    # Full reverse of criticals
    base = _mk_run(
        [(i, AdvEvent(f"t{i}", f"b{i}", critical=True)) for i in range(6)]
    )
    comp = _mk_run(
        [(i, AdvEvent(f"t{5 - i}", f"b{5 - i}", critical=True)) for i in range(6)]
    )
    cases.append(Case("reorder_full_reverse", "reorder", base, comp))

    # Adjacent swap cascade
    base = _mk_run(
        [(i, AdvEvent("step", f"s{i}", critical=True)) for i in range(8)]
    )
    order = list(range(8))
    for i in range(0, 7, 2):
        order[i], order[i + 1] = order[i + 1], order[i]
    comp = _mk_run(
        [(i, AdvEvent("step", f"s{order[i]}", critical=True)) for i in range(8)]
    )
    cases.append(Case("reorder_adjacent_swaps", "reorder", base, comp))
    return cases


def all_generator_cases() -> List[Case]:
    """Full v2 catalog: originals + expanded pathological families."""
    cases: List[Case] = []
    cases.append(duplicate_event_types())
    cases.append(repeated_tool_calls())
    cases.extend(near_identical_family())
    cases.extend(insert_delete_family())
    cases.extend(reorder_family())
    cases.extend(many_equally_scored_candidates())
    cases.extend(one_to_many_collision_family())
    cases.extend(critical_structural_priority_interactions())
    cases.append(long_repeated_patterns())  # legacy ~30
    for n in (50, 100, 150, 200):
        cases.append(long_repeated_patterns_n(n))
    cases.extend(nested_interleaved_duplicates())
    cases.extend(threshold_boundary_expanded())
    cases.extend(semantic_hook_family())
    cases.extend(greedy_trap_family())
    cases.extend(seeded_fuzz_families())
    # Deduplicate by name while preserving order.
    seen = set()
    unique: List[Case] = []
    for c in cases:
        if c.name in seen:
            continue
        seen.add(c.name)
        unique.append(c)
    return unique


# Cases that require non-ExactEquality evaluators in the runner.
SPECIAL_EVALUATOR_CASES = frozenset(
    {
        "greedy_trap_assignment",
        "greedy_trap_3x3",
        "greedy_trap_4x4",
        "greedy_trap_steal_column",
        "semantic_evaluator_disagree",
        "semantic_three_cluster_cross",
        "semantic_partial_cluster",
    }
)

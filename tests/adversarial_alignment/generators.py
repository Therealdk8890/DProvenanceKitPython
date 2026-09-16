"""Pathological trace pair generators for adversarial alignment testing."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence, Tuple

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


def duplicate_event_types() -> Case:
    base = _mk_run(
        [
            (0, AdvEvent("tool_call", "search:q1", critical=True)),
            (1, AdvEvent("tool_call", "search:q2", critical=True)),
            (2, AdvEvent("tool_call", "search:q3", critical=True)),
        ]
    )
    # Same types, different order + one near-miss decoy body.
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
    # Inserted decoys between repeats.
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
            (0, AdvEvent("decision", "authorize:alice:100 ", critical=True)),  # trailing space
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
    # Identical payloads for two candidates of same type → equal scores.
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
    # Force scores via identical types; exact body match beats near-miss.
    base = _mk_run(
        [
            (0, AdvEvent("decision", "weak-prefer-A", critical=True)),
            (1, AdvEvent("decision", "exact-B", critical=True)),
        ]
    )
    comp = _mk_run(
        [
            # C0 is exact for base1, near for base0
            (0, AdvEvent("decision", "exact-B", critical=True)),
            # C1 is exact for base0
            (1, AdvEvent("decision", "weak-prefer-A", critical=True)),
        ]
    )
    return Case("one_to_many_collisions", "collisions", base, comp)


def threshold_boundary_scores() -> List[Case]:
    """Cases around developer_debug semantic_threshold 0.75 and bind floor 0.4.

    With developer_debug weights (type 0.4, payload 0.4, structural 0.15, temporal 0.05),
    exact type+payload with no structural/temporal can land near 0.8. We also build a
    custom profile at 0.75 and use payload-only evaluators that return exact boundary
    values — those live in the test file; here we emit structural near-miss cases.
    """
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
    # Structural events move; criticals stay in relative order.
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
    pattern = ["plan", "tool_call", "observe", "tool_call", "conclude"]
    base_specs = []
    for cycle in range(6):
        for i, kind in enumerate(pattern):
            seq = cycle * len(pattern) + i
            crit = kind in ("plan", "conclude")
            base_specs.append(
                (seq, AdvEvent(kind, f"c{cycle}:{kind}", critical=crit))
            )
    # Drop one conclude mid-way, insert decoy tool_calls, reorder one cycle.
    comp_specs = []
    seq = 0
    for cycle in range(6):
        kinds = list(pattern)
        if cycle == 2:
            kinds = ["plan", "tool_call", "tool_call", "observe", "conclude"]  # decoy
        if cycle == 4:
            kinds = ["conclude", "plan", "tool_call", "observe", "tool_call"]  # reorder
        if cycle == 3:
            kinds = ["plan", "tool_call", "observe", "tool_call"]  # deleted conclude
        for kind in kinds:
            crit = kind in ("plan", "conclude")
            body = f"c{cycle}:{kind}"
            if cycle == 2 and kind == "tool_call" and seq % 2 == 0:
                body = f"c{cycle}:decoy"
            comp_specs.append((seq, AdvEvent(kind, body, critical=crit)))
            seq += 1
    return Case(
        "long_repeated_patterns",
        "long",
        _mk_run(base_specs),
        _mk_run(comp_specs),
    )


def semantic_evaluator_disagree_hooks() -> Case:
    """Documents the hook: evaluator can disagree with weighted type+payload score.

    The suite marks this case; whether a custom evaluator is injected is decided
    by the runner (see test file). Default payloads here are identical so the
    production ExactEquality path is a no-op baseline.
    """
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
    """Constructed so greedy highest-first differs from max-weight matching.

    Score landscape (ExactEquality / strict_audit, type 0.5 + payload 0.5):
      base0 vs c0: type match only → 0.5
      base0 vs c1: exact → 1.0
      base1 vs c0: exact → 1.0
      base1 vs c1: type match only → 0.5

    Greedy takes (0,1)=1.0 then (1,0)=1.0 — actually optimal too.
    Need asymmetric trap:

      base0-c0=0.9, base0-c1=0.8
      base1-c0=0.85, base1-c1=0.0 (below threshold)

    Greedy: (0,0)=0.9, base1 unbound. Optimal: (0,1)+(1,0)=1.65.
    With ExactEquality we only get 0.5 or 1.0, so use a graded evaluator in the
    runner for this named case; payloads encode intended grades.
    """
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


def all_generator_cases() -> List[Case]:
    cases: List[Case] = [
        duplicate_event_types(),
        repeated_tool_calls(),
        near_identical_payloads(),
        inserted_decoys(),
        deleted_events(),
        reordered_events(),
        equally_scored_candidates(),
        one_to_many_collisions(),
        critical_structural_mix(),
        long_repeated_patterns(),
        semantic_evaluator_disagree_hooks(),
        greedy_trap_assignment(),
    ]
    cases.extend(threshold_boundary_scores())
    return cases

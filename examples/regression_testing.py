#!/usr/bin/env python3
"""Worked example — catch a behavioral regression between two runs of an agent.

The headline use case for DProvenanceKit: you have a *golden* run of an agent that
behaves correctly, and you want to know when a later version of the agent quietly
changes its reasoning — skips a safety step, decides differently, reorders its work.

The agent here is a fact-checker that must:
    1. retrieve sources            (structural step)
    2. verify the claim against them   (CRITICAL — the safety step)
    3. make a decision             (CRITICAL)

We record a golden run, then two later runs, and show two complementary signals:

    • the run FINGERPRINT — a fast structural identity (Trace Spec v1 §5). Same path
      ⇒ same fingerprint; any change to the sequence of (step, engine) changes it.
    • ALIGNMENT — grades the difference: regression level + per-step verdict, pointing
      at exactly which step regressed.

Run it:

    python examples/regression_testing.py

It prints a report and asserts the expected verdicts, so it doubles as an executable
test of the end-to-end story.
"""

from __future__ import annotations

import hashlib
import os
import sys
import uuid
from dataclasses import dataclass
from typing import Optional

# Make the package importable when run straight from a checkout (no install needed).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from dprovenancekit import (  # noqa: E402
    AlignmentConfiguration,
    AlignmentProfile,
    AnyEquivalenceEvaluator,
    DProvenanceKit,
    InMemoryTraceStore,
    RegressionLevel,
    TraceableEvent,
    TraceAlignmentEngine,
    TracePriority,
    TraceRun,
)


# ── 1. Define the agent's event vocabulary ──────────────────────────────────────


@dataclass(frozen=True)
class FactCheckEvent(TraceableEvent):
    """One step in a fact-checking agent's reasoning."""

    kind: str
    detail: str = ""

    @property
    def type_identifier(self) -> str:
        return self.kind

    @property
    def priority(self) -> TracePriority:
        # The verification step and the decision are the reasoning we cannot lose;
        # losing one is a correctness regression, not just noise.
        if self.kind in ("claimVerified", "decisionMade"):
            return TracePriority.CRITICAL
        return TracePriority.STRUCTURAL

    def to_dict(self) -> dict:
        return {"kind": self.kind, "detail": self.detail}

    @classmethod
    def from_dict(cls, data: dict) -> "FactCheckEvent":
        return cls(kind=data["kind"], detail=data.get("detail", ""))

    # Factories for the three steps.
    @classmethod
    def sources_retrieved(cls, detail: str) -> "FactCheckEvent":
        return cls("sourcesRetrieved", detail)

    @classmethod
    def claim_verified(cls, detail: str) -> "FactCheckEvent":
        return cls("claimVerified", detail)

    @classmethod
    def decision_made(cls, detail: str) -> "FactCheckEvent":
        return cls("decisionMade", detail)


# ── 2. Run the agent, recording a trace ─────────────────────────────────────────


def run_fact_checker(
    store: InMemoryTraceStore,
    context_id: str,
    *,
    skip_verification: bool = False,
    decision: str = "supported",
) -> uuid.UUID:
    """Execute one fact-check, recording each step. Returns the run id.

    The two knobs let us simulate later, regressed versions of the agent:
    ``skip_verification`` drops the safety step; ``decision`` flips the outcome.
    """
    kit = DProvenanceKit(FactCheckEvent)
    with kit.run(context_id=context_id, store=store) as run:
        with kit.with_engine("Retriever"):
            kit.record(FactCheckEvent.sources_retrieved("3 sources"))
        if not skip_verification:
            with kit.with_engine("Verifier"):
                kit.record(FactCheckEvent.claim_verified("2 of 3 sources agree"))
        with kit.with_engine("Decider"):
            kit.record(FactCheckEvent.decision_made(decision))
        return run.run_id


# ── 3. The fast structural check: the run fingerprint (Trace Spec v1 §5) ─────────


def run_fingerprint(run: TraceRun) -> str:
    """SHA-1 over each event's ``type:engine|`` signature, in commit (sequence) order.

    This is the exact algorithm the SQLite store computes and the spec pins — a handful
    of lines, identical in any language. Two runs share a fingerprint iff they took the
    same typed steps through the same engines in the same order.
    """
    digest = hashlib.sha1()
    for event in sorted(run.events, key=lambda e: e.sequence):
        digest.update(f"{event.payload.type_identifier}:{event.engine_name or ''}|".encode())
    return digest.hexdigest()


# ── 4. Alignment: grade the difference ──────────────────────────────────────────


def build_engine() -> TraceAlignmentEngine:
    """Strict-audit alignment with exact-equality payload comparison.

    Exact equality (the canonical conformance evaluator) keeps the verdict crisp: two
    steps match only if their payloads are identical, so a flipped decision shows up.
    """
    config = AlignmentConfiguration(
        profile=AlignmentProfile.strict_audit_v1,
        equivalence_evaluator=AnyEquivalenceEvaluator(
            evaluator_identifier="ExactEquality_v1",
            evaluator=lambda a, b: 1.0 if a == b else 0.0,
        ),
    )
    return TraceAlignmentEngine(config)


def _step_label(alignment) -> str:
    event = alignment.base_event or alignment.comparison_event
    return event.payload.type_identifier if event else "?"


def compare(engine: TraceAlignmentEngine, golden: TraceRun, candidate: TraceRun) -> object:
    """Align ``candidate`` against ``golden`` and print a readable verdict."""
    result = engine.align(base=golden, comparison=candidate)
    risk = result.regression_risk
    print(f"    regression: {risk.level.value.upper()}  (strength {risk.strength:.2f})")
    for alignment in result.alignments:
        print(f"      - {_step_label(alignment):<16} {alignment.state.kind.value}")
    return result


# ── 5. The story ─────────────────────────────────────────────────────────────────


def main() -> None:
    store = InMemoryTraceStore()
    engine = build_engine()

    # The golden run: retrieve → verify → decide("supported").
    golden_id = run_fact_checker(store, "golden")
    golden = store.get_run(golden_id)
    golden_fp = run_fingerprint(golden)

    print("GOLDEN RUN")
    print(f"    steps: {[e.payload.type_identifier for e in golden.events]}")
    print(f"    fingerprint: {golden_fp}\n")

    # Candidate A — an identical re-run of the same agent. Should be a clean pass.
    same_id = run_fact_checker(store, "rerun")
    same = store.get_run(same_id)
    same_fp = run_fingerprint(same)
    print("CANDIDATE A — identical re-run")
    print(f"    fingerprint {'MATCHES' if same_fp == golden_fp else 'DIFFERS'} golden")
    result_same = compare(engine, golden, same)
    print()

    # Candidate B — a regressed agent that SKIPS verification before deciding.
    skipped_id = run_fact_checker(store, "skipped-verification", skip_verification=True)
    skipped = store.get_run(skipped_id)
    skipped_fp = run_fingerprint(skipped)
    print("CANDIDATE B — skips the verification step")
    print(f"    fingerprint {'MATCHES' if skipped_fp == golden_fp else 'DIFFERS'} golden")
    result_skipped = compare(engine, golden, skipped)
    print()

    # ── Assertions: this example is also a test of the end-to-end story. ──
    # A. Identical re-run: same fingerprint, no regression.
    assert same_fp == golden_fp, "an identical re-run must share the golden fingerprint"
    assert result_same.regression_risk.level is RegressionLevel.NONE

    # B. Skipped verification: different fingerprint, and alignment flags the dropped
    #    CRITICAL step as a high-risk regression.
    assert skipped_fp != golden_fp, "skipping a step must change the fingerprint"
    assert result_skipped.regression_risk.level is RegressionLevel.HIGH
    removed = [
        a for a in result_skipped.alignments if a.state.kind.value == "removed"
    ]
    assert any(_step_label(a) == "claimVerified" for a in removed), (
        "the dropped verification step must be reported as removed"
    )

    print("OK — clean re-run passes; skipped verification is caught as a HIGH regression.")


if __name__ == "__main__":
    main()

"""Tests for the drop-in regression gate (``dprovenancekit.testing``)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest

from dprovenancekit import (
    AlignmentProfile,
    AnyEquivalenceEvaluator,
    DProvenanceKit,
    InMemoryTraceStore,
    RegressionError,
    RegressionGate,
    RegressionLevel,
    TraceableEvent,
    TracePriority,
    assert_no_regression,
    run_fingerprint,
)


@dataclass(frozen=True)
class FCEvent(TraceableEvent):
    """A fact-checking step. ``verified`` and ``decided`` are the CRITICAL reasoning."""

    kind: str
    detail: str = ""

    @property
    def type_identifier(self) -> str:
        return self.kind

    @property
    def priority(self) -> TracePriority:
        if self.kind in ("verified", "decided"):
            return TracePriority.CRITICAL
        if self.kind == "note":
            return TracePriority.TELEMETRY
        return TracePriority.STRUCTURAL

    def to_dict(self) -> dict:
        return {"kind": self.kind, "detail": self.detail}

    @classmethod
    def from_dict(cls, data: dict) -> "FCEvent":
        return cls(kind=data["kind"], detail=data.get("detail", ""))


def build_run(
    store: InMemoryTraceStore,
    context_id: str,
    *,
    skip_verify: bool = False,
    skip_decide: bool = False,
    decision: str = "supported",
    retrieved_detail: str = "3 sources",
    extra_step: str = None,
    add_note: bool = False,
):
    kit = DProvenanceKit(FCEvent)
    with kit.run(context_id=context_id, store=store) as run:
        with kit.with_engine("Retriever"):
            kit.record(FCEvent("retrieved", retrieved_detail))
        if not skip_verify:
            with kit.with_engine("Verifier"):
                kit.record(FCEvent("verified", "2 of 3 agree"))
        if extra_step is not None:
            with kit.with_engine("Extra"):
                kit.record(FCEvent(extra_step, "x"))  # a STRUCTURAL step golden lacks
        if add_note:
            kit.record(FCEvent("note", "debug"))  # TELEMETRY — below the gate's floor
        if not skip_decide:
            with kit.with_engine("Decider"):
                kit.record(FCEvent("decided", decision))
        return store.get_run(run.run_id)


# ── Pass path ──────────────────────────────────────────────────────────────────


def test_identical_runs_pass():
    store = InMemoryTraceStore()
    golden = build_run(store, "golden")
    candidate = build_run(store, "candidate")

    report = RegressionGate().check(golden, candidate)
    assert report.passed
    assert report.regression_level is RegressionLevel.NONE
    assert report.strength == pytest.approx(1.0)
    assert report.fingerprint_match
    assert report.steps_by_change == {}
    # Policy-echo and raw-fingerprint fields are wired correctly.
    assert report.max_regression_level is RegressionLevel.NONE
    assert report.allow_divergent_steps is False
    assert report.golden_fingerprint == run_fingerprint(golden)
    assert report.candidate_fingerprint == run_fingerprint(candidate)
    # The PASS-side rendering of summary().
    summary = report.summary()
    assert "PASS" in summary and "match" in summary and "none (all exact matches)" in summary
    # The instance method returns the (passing) report; the module-level fn does too.
    assert RegressionGate().assert_no_regression(golden, candidate).passed
    assert assert_no_regression(golden, candidate).passed


# ── Fail path: a removed CRITICAL step ───────────────────────────────────────────


def test_skipped_verification_is_caught():
    store = InMemoryTraceStore()
    golden = build_run(store, "golden")
    regressed = build_run(store, "regressed", skip_verify=True)

    with pytest.raises(RegressionError) as excinfo:
        RegressionGate().assert_no_regression(golden, regressed)

    report = excinfo.value.report
    assert report.regression_level is RegressionLevel.HIGH
    assert "verified" in report.removed_steps
    assert not report.fingerprint_match


def test_added_step_is_caught():
    store = InMemoryTraceStore()
    golden = build_run(store, "golden")
    candidate = build_run(store, "candidate", extra_step="lookup")

    report = RegressionGate().check(golden, candidate)
    assert not report.passed
    assert report.added_steps == ["lookup"]
    assert "lookup" in report.divergent_steps
    # An added step is a divergence but not, by itself, a severity escalation.
    assert report.regression_level is RegressionLevel.NONE
    # Lenient mode tolerates it.
    assert RegressionGate(allow_divergent_steps=True).check(golden, candidate).passed


def test_multiple_critical_removals():
    store = InMemoryTraceStore()
    golden = build_run(store, "golden")
    regressed = build_run(store, "regressed", skip_verify=True, skip_decide=True)

    report = RegressionGate().check(golden, regressed)
    assert report.regression_level is RegressionLevel.HIGH
    assert report.removed_steps == ["verified", "decided"]  # base sequence order
    assert "verified" in report.reasoning and "decided" in report.reasoning


# ── Lenient policies ─────────────────────────────────────────────────────────────


def test_allow_divergent_tolerates_a_changed_payload():
    store = InMemoryTraceStore()
    golden = build_run(store, "golden", decision="supported")
    flipped = build_run(store, "flipped", decision="refuted")

    # Strict (default): the changed decision is an ambiguous divergence → fail.
    strict = RegressionGate().check(golden, flipped)
    assert not strict.passed
    assert "decided" in strict.divergent_steps

    # Lenient: tolerate per-step changes, gate only on severity → pass (no critical removal).
    lenient = RegressionGate(allow_divergent_steps=True).check(golden, flipped)
    assert lenient.passed
    assert lenient.regression_level is RegressionLevel.NONE


def test_lenient_still_catches_critical_removal_unless_level_raised():
    store = InMemoryTraceStore()
    golden = build_run(store, "golden")
    regressed = build_run(store, "regressed", skip_verify=True)

    # Tolerating divergent steps does NOT excuse a HIGH-severity critical removal.
    gate = RegressionGate(allow_divergent_steps=True, max_regression_level=RegressionLevel.NONE)
    assert not gate.check(golden, regressed).passed

    # Explicitly raising the severity ceiling to HIGH lets it pass.
    permissive = RegressionGate(
        allow_divergent_steps=True, max_regression_level=RegressionLevel.HIGH
    )
    assert permissive.check(golden, regressed).passed


def test_custom_evaluator_defines_equivalence():
    store = InMemoryTraceStore()
    golden = build_run(store, "golden", decision="supported")
    flipped = build_run(store, "flipped", decision="refuted")

    # An evaluator that treats everything as equivalent → the flipped decision is a
    # semanticMatch, which the strict gate accepts (it is not a divergence).
    always_equal = AnyEquivalenceEvaluator(
        evaluator_identifier="AlwaysEqual_test", evaluator=lambda a, b: 1.0
    )
    report = RegressionGate(evaluator=always_equal).check(golden, flipped)
    assert report.passed
    assert "decided" in report.steps_by_change.get("semanticMatch", [])


# ── minimum_priority ─────────────────────────────────────────────────────────────


def test_telemetry_noise_does_not_trip_the_gate():
    store = InMemoryTraceStore()
    golden = build_run(store, "golden")
    noisy = build_run(store, "noisy", add_note=True)

    report = RegressionGate().check(golden, noisy)
    # Alignment ignores sub-STRUCTURAL events, so the verdict is clean...
    assert report.passed
    # ...even though the coarse fingerprint (all events) differs because of the note.
    assert not report.fingerprint_match


def test_custom_minimum_priority_is_honored():
    store = InMemoryTraceStore()
    golden = build_run(store, "golden")
    # Candidate differs only in the STRUCTURAL "retrieved" step.
    candidate = build_run(store, "candidate", retrieved_detail="5 sources")

    # Default floor (STRUCTURAL) sees the structural change → fail.
    assert not RegressionGate().check(golden, candidate).passed
    # Raising the floor to CRITICAL filters that step out → pass.
    lifted = RegressionGate(minimum_priority=TracePriority.CRITICAL).check(golden, candidate)
    assert lifted.passed


# ── Reorder detection depends on the profile (documented limitation) ─────────────


def test_reordering_only_caught_with_a_span_aware_profile():
    store = InMemoryTraceStore()
    golden = build_run(store, "golden")  # retrieved, verified, decided

    # Same steps and payloads, but `decided` is emitted before `verified`.
    kit = DProvenanceKit(FCEvent)
    with kit.run(context_id="reordered", store=store) as run:
        with kit.with_engine("Retriever"):
            kit.record(FCEvent("retrieved", "3 sources"))
        with kit.with_engine("Decider"):
            kit.record(FCEvent("decided", "supported"))
        with kit.with_engine("Verifier"):
            kit.record(FCEvent("verified", "2 of 3 agree"))
        reordered = store.get_run(run.run_id)

    # The default linear profile does NOT catch a pure reorder (it binds 1:1).
    assert RegressionGate().check(golden, reordered).passed
    # A span-aware profile does.
    span_aware = RegressionGate(profile=AlignmentProfile.developer_debug_v1)
    assert not span_aware.check(golden, reordered).passed


# ── Report + convenience surface ─────────────────────────────────────────────────


def test_report_summary_is_readable():
    store = InMemoryTraceStore()
    golden = build_run(store, "golden")
    regressed = build_run(store, "regressed", skip_verify=True)

    summary = RegressionGate().check(golden, regressed).summary()
    assert "FAIL" in summary
    assert "verified" in summary
    assert "removed" in summary


def test_module_level_assert_raises_on_regression():
    store = InMemoryTraceStore()
    golden = build_run(store, "golden")
    regressed = build_run(store, "regressed", skip_verify=True)
    with pytest.raises(RegressionError):
        assert_no_regression(golden, regressed)


def test_run_fingerprint_tracks_structural_path():
    store = InMemoryTraceStore()
    golden = build_run(store, "golden")
    same = build_run(store, "same")
    skipped = build_run(store, "skipped", skip_verify=True)

    assert run_fingerprint(golden) == run_fingerprint(same)
    assert run_fingerprint(golden) != run_fingerprint(skipped)


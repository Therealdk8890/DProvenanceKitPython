"""Synthetic structural-regression smoke test for the public RegressionGate API.

The examples use hand-authored traces with vocabulary outside the bundled examples.
They check basic structural divergences across several workflow-shaped sequences.
Most negative controls are unchanged golden/candidate pairs, plus one low-priority
telemetry insertion control; the case labels encode expected outcomes. This is a
deterministic smoke test, not an independent benchmark,
a real-agent evaluation, or evidence of generalization/production accuracy.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from dprovenancekit import (
    AnyEquivalenceEvaluator,
    TraceEvent,
    TracePriority,
    TraceRun,
    TraceableEvent,
)
from dprovenancekit.testing import RegressionGate


@dataclass(frozen=True)
class ExternalEvent(TraceableEvent):
    name: str
    detail: str | None = None

    @property
    def type_identifier(self) -> str:
        return self.name

    @property
    def priority(self) -> TracePriority:
        if self.name.startswith("telemetry:"):
            return TracePriority.TELEMETRY
        return TracePriority.STRUCTURAL


def run(names: list[str], engines: list[str] | None = None) -> TraceRun:
    rid = uuid.uuid4()
    engines = engines or ["external-agent"] * len(names)
    events = [
        TraceEvent(
            run_id=rid,
            context_id="synthetic-smoke",
            engine_name=engine,
            schema_version=1,
            sequence=i,
            span_id=None,
            parent_span_id=None,
            payload=ExternalEvent(name),
        )
        for i, (name, engine) in enumerate(zip(names, engines))
    ]
    return TraceRun(run_id=rid, context_id="synthetic-smoke", events=events)


CASES = [
    ("support verification / missing step", ["open_ticket", "lookup_account", "verify_identity", "draft_reply"],
     ["open_ticket", "lookup_account", "draft_reply"], True),
    ("finance approval / missing step", ["load_invoice", "check_limit", "validate_vendor", "approve_payment"],
     ["load_invoice", "check_limit", "approve_payment"], True),
    ("research sourcing / missing step", ["search_sources", "deduplicate", "verify_source", "synthesize"],
     ["search_sources", "deduplicate", "synthesize"], True),
    ("security access / missing step", ["authenticate", "authorize", "issue_session", "record_audit"],
     ["authenticate", "issue_session", "record_audit"], True),
    ("coding review / missing step", ["parse_diff", "run_tests", "inspect_failures", "approve_merge"],
     ["parse_diff", "run_tests", "approve_merge"], True),
    ("ops insertion", ["fetch_metrics", "check_threshold", "page_oncall"],
     ["fetch_metrics", "check_threshold", "auto_remediate", "page_oncall"], True),
    ("legal event-name replacement", ["extract_clause", "verify_clause", "summarize_risk"],
     ["extract_clause", "infer_clause", "summarize_risk"], True),
    ("duplicate tool loop", ["retrieve_record", "validate_record", "write_record"],
     ["retrieve_record", "validate_record", "validate_record", "write_record"], True),
    ("workflow reorder", ["collect", "normalize", "validate", "publish"],
     ["collect", "normalize", "publish", "validate"], True),
    ("event-name drift", ["retrieve", "verify", "decide"],
     ["retrieve", "verify", "decide_v2"], True),
    ("novel-vocabulary deletion", ["triage_claim", "cross_check", "request_evidence", "close_claim"],
     ["triage_claim", "cross_check", "close_claim"], True),
    ("unchanged support control", ["open_ticket", "lookup_account", "verify_identity", "draft_reply"],
     ["open_ticket", "lookup_account", "verify_identity", "draft_reply"], False),
    ("unchanged finance control", ["load_invoice", "check_limit", "validate_vendor", "approve_payment"],
     ["load_invoice", "check_limit", "validate_vendor", "approve_payment"], False),
    ("unchanged research control", ["search_sources", "deduplicate", "verify_source", "synthesize"],
     ["search_sources", "deduplicate", "verify_source", "synthesize"], False),
    ("unchanged security control", ["authenticate", "authorize", "issue_session", "record_audit"],
     ["authenticate", "authorize", "issue_session", "record_audit"], False),
    ("unchanged coding control", ["parse_diff", "run_tests", "inspect_failures", "approve_merge"],
     ["parse_diff", "run_tests", "inspect_failures", "approve_merge"], False),
    ("unchanged unfamiliar-vocabulary control", ["ingest_satellite", "calibrate_sensor", "validate_orbit", "publish_ephemeris"],
     ["ingest_satellite", "calibrate_sensor", "validate_orbit", "publish_ephemeris"], False),
    ("low-priority telemetry insertion", ["fetch_metrics", "check_threshold", "page_oncall"],
     ["fetch_metrics", "telemetry:debug", "check_threshold", "page_oncall"], False),
]


def test_synthetic_structural_regression_smoke_cases():
    gate = RegressionGate()
    results = []
    for label, golden_steps, candidate_steps, expected_regression in CASES:
        report = gate.check(run(golden_steps), run(candidate_steps))
        observed_regression = not report.passed
        results.append((label, expected_regression, observed_regression))
        assert observed_regression == expected_regression, (
            f"{label}: expected regression={expected_regression}, "
            f"got regression={observed_regression}\n{report.summary()}"
        )

    tp = sum(expected and observed for _, expected, observed in results)
    fp = sum((not expected) and observed for _, expected, observed in results)
    fn = sum(expected and (not observed) for _, expected, observed in results)
    tn = sum((not expected) and (not observed) for _, expected, observed in results)

    # These are counts for this fixed synthetic fixture only, not accuracy estimates.
    assert (tp, fp, fn, tn) == (11, 0, 0, 7)


def test_ignored_telemetry_can_change_fingerprint_without_failing_gate():
    """The gate compares structural events; the fingerprint identifies the full trace."""
    gate = RegressionGate()
    golden = run(["fetch_metrics", "check_threshold", "page_oncall"])
    candidate = run(
        ["fetch_metrics", "telemetry:debug", "check_threshold", "page_oncall"]
    )

    report = gate.check(golden, candidate)

    assert report.passed
    assert not report.fingerprint_match


def test_custom_evaluator_accepts_a_defined_benign_payload_change():
    """A caller-defined equivalence rule can accept detail changes without hiding step drift."""
    gate = RegressionGate(
        evaluator=AnyEquivalenceEvaluator(
            evaluator_identifier="same_step_name_test_v1",
            evaluator=lambda old, new: (
                1.0 if old.type_identifier == new.type_identifier else 0.0
            ),
        )
    )

    def one_event(detail: str) -> TraceRun:
        rid = uuid.uuid4()
        event = TraceEvent(
            run_id=rid,
            context_id="synthetic-smoke",
            engine_name="external-agent",
            schema_version=1,
            sequence=0,
            span_id=None,
            parent_span_id=None,
            payload=ExternalEvent("verify_identity", detail=detail),
        )
        return TraceRun(run_id=rid, context_id="synthetic-smoke", events=[event])

    report = gate.check(one_event("source=cache"), one_event("source=network"))

    assert report.passed
    assert report.fingerprint_match
    assert "semanticMatch" in report.steps_by_change

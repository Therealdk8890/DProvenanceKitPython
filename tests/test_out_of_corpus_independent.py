"""Independent out-of-corpus regression probe.

These cases are intentionally unrelated to the bundled benchmark vocabulary.
They exercise the public RegressionGate API against synthetic traces representing
support, finance, research, security, coding, and operations workflows.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from dprovenancekit import TraceEvent, TracePriority, TraceRun, TraceableEvent
from dprovenancekit.testing import RegressionGate


@dataclass(frozen=True)
class ExternalEvent(TraceableEvent):
    name: str

    @property
    def type_identifier(self) -> str:
        return self.name

    @property
    def priority(self) -> TracePriority:
        return TracePriority.STRUCTURAL


def run(names: list[str], engines: list[str] | None = None) -> TraceRun:
    rid = uuid.uuid4()
    engines = engines or ["external-agent"] * len(names)
    events = [
        TraceEvent(
            run_id=rid,
            context_id="independent",
            engine_name=engine,
            schema_version=1,
            sequence=i,
            span_id=None,
            parent_span_id=None,
            payload=ExternalEvent(name),
        )
        for i, (name, engine) in enumerate(zip(names, engines))
    ]
    return TraceRun(run_id=rid, context_id="independent", events=events)


CASES = [
    ("support verification", ["open_ticket", "lookup_account", "verify_identity", "draft_reply"],
     ["open_ticket", "lookup_account", "draft_reply"], True),
    ("finance approval", ["load_invoice", "check_limit", "validate_vendor", "approve_payment"],
     ["load_invoice", "check_limit", "approve_payment"], True),
    ("research sourcing", ["search_sources", "deduplicate", "verify_source", "synthesize"],
     ["search_sources", "deduplicate", "synthesize"], True),
    ("security access", ["authenticate", "authorize", "issue_session", "record_audit"],
     ["authenticate", "issue_session", "record_audit"], True),
    ("coding review", ["parse_diff", "run_tests", "inspect_failures", "approve_merge"],
     ["parse_diff", "run_tests", "approve_merge"], True),
    ("ops insertion", ["fetch_metrics", "check_threshold", "page_oncall"],
     ["fetch_metrics", "check_threshold", "auto_remediate", "page_oncall"], True),
    ("legal replacement", ["extract_clause", "verify_clause", "summarize_risk"],
     ["extract_clause", "infer_clause", "summarize_risk"], True),
    ("duplicate tool loop", ["retrieve_record", "validate_record", "write_record"],
     ["retrieve_record", "validate_record", "validate_record", "write_record"], True),
    ("workflow reorder", ["collect", "normalize", "validate", "publish"],
     ["collect", "normalize", "publish", "validate"], True),
    ("engine/name drift", ["retrieve", "verify", "decide"],
     ["retrieve", "verify", "decide_v2"], True),
    ("novel deletion", ["triage_claim", "cross_check", "request_evidence", "close_claim"],
     ["triage_claim", "cross_check", "close_claim"], True),
    ("identical support", ["open_ticket", "lookup_account", "verify_identity", "draft_reply"],
     ["open_ticket", "lookup_account", "verify_identity", "draft_reply"], False),
    ("identical finance", ["load_invoice", "check_limit", "validate_vendor", "approve_payment"],
     ["load_invoice", "check_limit", "validate_vendor", "approve_payment"], False),
    ("identical research", ["search_sources", "deduplicate", "verify_source", "synthesize"],
     ["search_sources", "deduplicate", "verify_source", "synthesize"], False),
    ("identical security", ["authenticate", "authorize", "issue_session", "record_audit"],
     ["authenticate", "authorize", "issue_session", "record_audit"], False),
    ("identical coding", ["parse_diff", "run_tests", "inspect_failures", "approve_merge"],
     ["parse_diff", "run_tests", "inspect_failures", "approve_merge"], False),
    ("novel vocabulary", ["ingest_satellite", "calibrate_sensor", "validate_orbit", "publish_ephemeris"],
     ["ingest_satellite", "calibrate_sensor", "validate_orbit", "publish_ephemeris"], False),
]


def test_independent_cases_outside_bundled_corpus():
    gate = RegressionGate()
    results = []
    for label, golden_steps, candidate_steps, expected_regression in CASES:
        report = gate.check(run(golden_steps), run(candidate_steps))
        observed_regression = not report.passed
        results.append((label, expected_regression, observed_regression, report.reasoning))
        assert observed_regression == expected_regression, (
            f"{label}: expected regression={expected_regression}, "
            f"got regression={observed_regression}\n{report.summary()}"
        )

    tp = sum(e and o for _, e, o, _ in results)
    fp = sum((not e) and o for _, e, o, _ in results)
    fn = sum(e and (not o) for _, e, o, _ in results)
    tn = sum((not e) and (not o) for _, e, o, _ in results)

    assert tp == 11
    assert fp == 0
    assert fn == 0
    assert tn == 6

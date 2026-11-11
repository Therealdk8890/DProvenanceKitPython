#!/usr/bin/env python3
"""Measure policy sensitivity on a small, manually labeled synthetic trace corpus.

This is NOT a production false-positive rate or a comparison to other vendors.
Each candidate has an independent ground-truth label; the production RegressionGate
is run unchanged under two documented policies.
"""

from __future__ import annotations

import argparse
import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple

from dprovenancekit import (
    RegressionGate,
    TraceEvent,
    TracePriority,
    TraceRun,
    TraceableEvent,
)

Step = Tuple[str, str, TracePriority]


@dataclass(frozen=True)
class WorkflowStep(TraceableEvent):
    name: str
    detail: str
    tier: TracePriority

    @property
    def type_identifier(self) -> str:
        return self.name

    @property
    def priority(self) -> TracePriority:
        return self.tier

    def to_dict(self) -> dict:
        return {"name": self.name, "detail": self.detail, "tier": int(self.tier)}

    @classmethod
    def from_dict(cls, data: dict) -> "WorkflowStep":
        return cls(data["name"], data["detail"], TracePriority(data["tier"]))


@dataclass(frozen=True)
class Scenario:
    name: str
    expected_regression: bool
    golden_steps: Tuple[Step, ...]
    candidate_steps: Tuple[Step, ...]


RETRIEVE: Step = ("retrieve", "three sources", TracePriority.STRUCTURAL)
VERIFY: Step = ("verify", "passed", TracePriority.CRITICAL)
DECIDE: Step = ("decide", "approve", TracePriority.CRITICAL)
BASE: Tuple[Step, ...] = (RETRIEVE, VERIFY, DECIDE)

# Ground truth is intentionally authored separately from production gate verdicts.
# The 'independent' order change is benign only under the stated domain assumption.
SCENARIOS: Tuple[Scenario, ...] = (
    Scenario("identical_rerun", False, BASE, BASE),
    Scenario(
        "benign_retrieval_payload", False, BASE,
        (("retrieve", "four sources", TracePriority.STRUCTURAL), VERIFY, DECIDE),
    ),
    Scenario(
        "benign_telemetry_added", False, BASE,
        BASE + (("token_count", "923", TracePriority.TELEMETRY),),
    ),
    Scenario(
        "benign_optional_structural_step", False, BASE,
        (RETRIEVE, ("cache_lookup", "hit", TracePriority.STRUCTURAL), VERIFY, DECIDE),
    ),
    Scenario(
        "benign_independent_critical_reorder", False,
        (RETRIEVE, ("format_check", "ok", TracePriority.CRITICAL), VERIFY, DECIDE),
        (RETRIEVE, VERIFY, ("format_check", "ok", TracePriority.CRITICAL), DECIDE),
    ),
    Scenario("regression_verification_removed", True, BASE, (RETRIEVE, DECIDE)),
    Scenario(
        "regression_decision_flipped", True, BASE,
        (RETRIEVE, VERIFY, ("decide", "reject", TracePriority.CRITICAL)),
    ),
    Scenario("regression_critical_dependency_reversed", True, BASE, (RETRIEVE, DECIDE, VERIFY)),
    Scenario("regression_decision_removed", True, BASE, (RETRIEVE, VERIFY)),
    Scenario("regression_both_critical_steps_removed", True, BASE, (RETRIEVE,)),
)


def _make_run(scenario: str, side: str, steps: Tuple[Step, ...]) -> TraceRun:
    run_id = uuid.uuid5(uuid.NAMESPACE_URL, "dpk-gate-policy-v1/" + scenario + "/" + side)
    events = [
        TraceEvent(
            run_id=run_id,
            context_id=scenario,
            engine_name="Agent",
            schema_version=1,
            sequence=index,
            span_id="root",
            parent_span_id=None,
            payload=WorkflowStep(name, detail, tier),
        )
        for index, (name, detail, tier) in enumerate(steps)
    ]
    return TraceRun(run_id=run_id, context_id=scenario, events=events)


def _policy_result(allow_divergent_steps: bool) -> dict:
    gate = RegressionGate(allow_divergent_steps=allow_divergent_steps)
    tp = fp = tn = fn = 0
    rows: List[dict] = []
    for scenario in SCENARIOS:
        golden = _make_run(scenario.name, "golden", scenario.golden_steps)
        candidate = _make_run(scenario.name, "candidate", scenario.candidate_steps)
        report = gate.check(golden, candidate)
        detected = not report.passed
        if scenario.expected_regression and detected:
            tp += 1
        elif scenario.expected_regression:
            fn += 1
        elif detected:
            fp += 1
        else:
            tn += 1
        rows.append({
            "name": scenario.name,
            "expected_regression": scenario.expected_regression,
            "blocked": detected,
            "severity": report.regression_level.value,
        })
    return {
        "confusion_matrix": {"tp": tp, "fp": fp, "tn": tn, "fn": fn},
        "false_positive_rate": fp / (fp + tn) if fp + tn else None,
        "false_negative_rate": fn / (fn + tp) if fn + tp else None,
        "false_positive_cases": [r["name"] for r in rows if r["blocked"] and not r["expected_regression"]],
        "missed_regression_cases": [r["name"] for r in rows if not r["blocked"] and r["expected_regression"]],
        "cases": rows,
    }


def run_benchmark() -> dict:
    return {
        "schema": "dpk-synthetic-gate-policy-v1",
        "scope": "Hand-labeled synthetic traces; not field performance",
        "scenario_count": len(SCENARIOS),
        "policies": {
            "strict_default": _policy_result(False),
            "severity_only": _policy_result(True),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Write JSON report to this file (default: stdout)")
    parser.add_argument(
        "--fail-on-missed-regression",
        action="store_true",
        help="Return 1 if either policy misses a labeled regression",
    )
    args = parser.parse_args()
    report = run_benchmark()
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    else:
        print(encoded, end="")
    if args.fail_on_missed_regression and any(
        policy["confusion_matrix"]["fn"] for policy in report["policies"].values()
    ):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

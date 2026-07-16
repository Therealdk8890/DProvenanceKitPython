"""Installed end-to-end demo for DProvenanceKit.

The demo deliberately uses only the public, dependency-free SDK surface. It records a
healthy agent run and a regressed candidate, then exercises querying, regression gating,
anomaly detection, structural diffing, HTML reporting, and the SQLite/CLI handoff.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
from dataclasses import dataclass

from .anomaly import AnomalyDetector
from .diff import TraceDiffEngine
from .event import AnyTraceableEvent, TraceableEvent
from .kit import DProvenanceKit
from .priority import TracePriority
from .query import TraceQueryDSL
from .report import render_report_html
from .rules import LoopingRule, ToolDropRule
from .sqlite_store import SQLiteTraceStore
from .store import InMemoryTraceStore
from .testing import RegressionGate


@dataclass(frozen=True)
class AgentStep(TraceableEvent):
    """A minimal agent event used by the installed demo."""

    action: str
    detail: str = ""

    @property
    def type_identifier(self) -> str:
        return self.action

    @property
    def priority(self) -> TracePriority:
        if self.action in ("verify", "decide"):
            return TracePriority.CRITICAL
        return TracePriority.STRUCTURAL

    def to_dict(self) -> dict:
        return {"action": self.action, "detail": self.detail}

    @classmethod
    def from_dict(cls, data: dict) -> "AgentStep":
        return cls(action=data["action"], detail=data.get("detail", ""))


GOLDEN_STEPS = [
    ("Planner", "plan", "decompose: 'what is the refund window?'"),
    ("Retriever", "search", "refund policy docs"),
    ("Retriever", "rank", "top 3 by relevance"),
    ("Verifier", "verify", "2 of 3 sources agree"),
    ("Planner", "decide", "answer: yes, within 30 days"),
]

CANDIDATE_STEPS = (
    [("Planner", "plan", "decompose: 'what is the refund window?'")]
    + [("Retriever", "search", "retry {}".format(i + 1)) for i in range(6)]
    + [("Planner", "decide", "answer: maybe 14 days?")]
)

DEMO_RULES = {
    "rules": [
        {"type": "tool_drop", "required_step": "verify"},
        {"type": "looping", "step": "search", "max_repeats": 3},
    ]
}


def _record(store, context_id, steps):
    kit = DProvenanceKit(AgentStep)
    with kit.run(context_id=context_id, store=store) as run:
        for engine, action, detail in steps:
            with kit.with_engine(engine):
                kit.record(AgentStep(action, detail))
        return store.get_run(run.run_id)


def _record_erased(store, context_id, steps):
    kit = DProvenanceKit(AnyTraceableEvent)
    with kit.run(context_id=context_id, store=store) as run:
        for engine, action, detail in steps:
            priority = (
                TracePriority.CRITICAL
                if action in ("verify", "decide")
                else TracePriority.STRUCTURAL
            )
            with kit.with_engine(engine):
                kit.record(
                    AnyTraceableEvent(
                        type_identifier_value=action,
                        priority_value=int(priority),
                        raw_json=json.dumps({"detail": detail}),
                    )
                )
        return run.run_id


def _banner(log, number, title):
    log("")
    log("── {}. {} ".format(number, title) + "─" * max(2, 60 - len(title)))


def _display_path(path):
    """Return a shell-safe path relative to the caller's working directory."""
    return shlex.quote(os.path.relpath(path, os.getcwd()))


def run_demo(output_dir, log=print):
    """Run the complete demo and write its three self-contained artifacts."""
    output_dir = os.path.abspath(output_dir)
    os.makedirs(output_dir, exist_ok=True)

    _banner(log, 1, "Record two runs of the agent")
    store = InMemoryTraceStore()
    golden = _record(store, "research-agent · main", GOLDEN_STEPS)
    candidate = _record(store, "research-agent · PR-42", CANDIDATE_STEPS)
    log(
        "   golden:    {} steps  {}".format(
            len(golden.events), [e.payload.type_identifier for e in golden.events]
        )
    )
    log(
        "   candidate: {} steps  {}".format(
            len(candidate.events), [e.payload.type_identifier for e in candidate.events]
        )
    )

    _banner(log, 2, "Query for a suspicious pattern (searched but never verified)")
    suspicious = store.query_runs(
        TraceQueryDSL().requiring_step("search").missing_step("verify")
    )
    log("   matched {} run(s): {}".format(len(suspicious), [r.context_id for r in suspicious]))

    _banner(log, 3, "Gate the candidate against the golden run")
    report = RegressionGate().check(golden, candidate)
    log(
        "   verdict: {}  (severity {}, strength {:.2f})".format(
            "PASS" if report.passed else "REGRESSION",
            report.regression_level.value,
            report.strength,
        )
    )
    log("   removed critical steps: {}".format(report.removed_steps))

    _banner(log, 4, "Run out-of-the-box anomaly rules over every recorded run")
    rules = [ToolDropRule("verify"), LoopingRule("search", max_repeats=3)]
    anomalies = AnomalyDetector(store).detect_anomalies(rules)
    for anomaly in anomalies:
        log("   ! [{}] {}".format(anomaly.rule_name, anomaly.description))

    _banner(log, 5, "Structural diff of the two runs")
    diff = TraceDiffEngine().diff(base=golden, comparison=candidate)
    for change in diff.changes:
        log(
            "   {:<7} {} ({}) @seq {}".format(
                change.kind.value,
                change.type_identifier,
                change.engine_name,
                change.original_sequence,
            )
        )

    _banner(log, 6, "Export a shareable HTML report")
    html = render_report_html(
        report,
        golden_label="research-agent · main",
        candidate_label="research-agent · PR-42",
    )
    report_path = os.path.join(output_dir, "demo-report.html")
    with open(report_path, "w", encoding="utf-8") as handle:
        handle.write(html)
    log("   wrote {}  (open it, or Print → Save as PDF)".format(_display_path(report_path)))

    rules_path = os.path.join(output_dir, "demo-rules.json")
    with open(rules_path, "w", encoding="utf-8") as handle:
        json.dump(DEMO_RULES, handle, indent=2)
        handle.write("\n")

    _banner(log, 7, "Take the same runs to CI")
    db_path = os.path.join(output_dir, "demo-traces.sqlite")
    for extension in ("", "-wal", "-shm"):
        try:
            os.remove(db_path + extension)
        except OSError:
            pass
    sql = SQLiteTraceStore(AnyTraceableEvent, db_path, start_writer=False)
    golden_id = _record_erased(sql, "research-agent · main", GOLDEN_STEPS)
    candidate_id = _record_erased(sql, "research-agent · PR-42", CANDIDATE_STEPS)
    sql.close()

    db_arg = _display_path(db_path)
    rules_arg = _display_path(rules_path)
    log(
        "   wrote {}  (golden={}  candidate={})".format(
            db_arg, str(golden_id)[:8], str(candidate_id)[:8]
        )
    )
    log("   wrote {}".format(rules_arg))
    log("")
    log("   Gate it in CI (the same engine the GitHub Action / GitLab template wrap):")
    log(
        "     dprovenancekit gate --db {} --golden {} --candidate {}".format(
            db_arg, golden_id, candidate_id
        )
    )
    log("   Run the anomaly rules:")
    log("     dprovenancekit anomalies --db {} --rules {}".format(db_arg, rules_arg))
    log("   List / select runs (baseline selection):")
    log("     dprovenancekit runs --db {} --latest --format id".format(db_arg))
    log("   Visualize locally:")
    log("     dprovenancekit ui --db {}".format(db_arg))

    assert not report.passed, "expected the candidate to be a REGRESSION"
    assert "verify" in report.removed_steps
    assert {a.rule_name for a in anomalies} == {"tool_drop:verify", "looping:search"}
    assert not diff.is_identical
    log("")
    log("OK — every layer agreed: the candidate regressed (dropped verify, looped search).")

    return {
        "gate_passed": report.passed,
        "removed": report.removed_steps,
        "anomaly_rules": sorted(a.rule_name for a in anomalies),
        "report_path": report_path,
        "rules_path": rules_path,
        "db_path": db_path,
        "golden_id": str(golden_id),
        "candidate_id": str(candidate_id),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="dprovenancekit demo",
        description="Run the installed end-to-end agent regression demo.",
    )
    parser.add_argument(
        "--output-dir",
        help="artifact directory (default: current working directory)",
    )
    args = parser.parse_args(argv)
    output_dir = args.output_dir or os.environ.get("DPROV_DEMO_OUT") or os.getcwd()

    print("DProvenanceKit — end-to-end demo")
    print("=" * 64)
    run_demo(output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

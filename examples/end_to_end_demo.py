#!/usr/bin/env python3
"""End-to-end demo — the whole DProvenanceKit arc in one runnable script.

    Record → Query → Gate → Detect anomalies → Diff → Report,
    then hand the same runs to the CLI and CI.

A research agent answers a support question. We record a healthy *golden* run and a
*regressed* candidate — it looped its search tool and skipped the verification step — then
watch every layer catch the regression. Run it:

    python examples/end_to_end_demo.py

It writes an HTML report and a SQLite trace database next to this script, prints the exact
CLI / CI commands to use them, and self-asserts the expected verdicts so it
doubles as an executable end-to-end test.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass

# Run from a checkout without installing: make src/ importable.
_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC) and _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from dprovenancekit import (  # noqa: E402
    AnomalyDetector,
    AnyTraceableEvent,
    DProvenanceKit,
    InMemoryTraceStore,
    LoopingRule,
    RegressionGate,
    SQLiteTraceStore,
    ToolDropRule,
    TraceableEvent,
    TraceDiffEngine,
    TracePriority,
    TraceQueryDSL,
    render_report_html,
)


# ── the agent's event model (idiomatic: a frozen dataclass) ──────────────────────


@dataclass(frozen=True)
class AgentStep(TraceableEvent):
    action: str  # plan | search | rank | verify | decide
    detail: str = ""

    @property
    def type_identifier(self) -> str:
        return self.action

    @property
    def priority(self) -> TracePriority:
        # verify + decide are the load-bearing reasoning — never silently dropped.
        return TracePriority.CRITICAL if self.action in ("verify", "decide") else TracePriority.STRUCTURAL

    def to_dict(self) -> dict:
        return {"action": self.action, "detail": self.detail}

    @classmethod
    def from_dict(cls, data: dict) -> "AgentStep":
        return cls(action=data["action"], detail=data.get("detail", ""))


# (engine, action, detail) for each step.
GOLDEN_STEPS = [
    ("Planner", "plan", "decompose: 'what is the refund window?'"),
    ("Retriever", "search", "refund policy docs"),
    ("Retriever", "rank", "top 3 by relevance"),
    ("Verifier", "verify", "2 of 3 sources agree"),
    ("Planner", "decide", "answer: yes, within 30 days"),
]
# The regressed run looped its search tool and skipped verification.
CANDIDATE_STEPS = (
    [("Planner", "plan", "decompose: 'what is the refund window?'")]
    + [("Retriever", "search", f"retry {i + 1}") for i in range(6)]
    + [("Planner", "decide", "answer: maybe 14 days?")]
)


def _record(store, context_id, steps):
    """Record a scenario with the idiomatic concrete event type."""
    kit = DProvenanceKit(AgentStep)
    with kit.run(context_id=context_id, store=store) as run:
        for engine, action, detail in steps:
            with kit.with_engine(engine):
                kit.record(AgentStep(action, detail))
        return store.get_run(run.run_id)


def _record_erased(store, context_id, steps):
    """Record the same scenario type-erased (``AnyTraceableEvent``) — the on-disk / wire form
    the CLI reads."""
    kit = DProvenanceKit(AnyTraceableEvent)
    with kit.run(context_id=context_id, store=store) as run:
        for engine, action, detail in steps:
            prio = TracePriority.CRITICAL if action in ("verify", "decide") else TracePriority.STRUCTURAL
            with kit.with_engine(engine):
                kit.record(AnyTraceableEvent(
                    type_identifier_value=action,
                    priority_value=int(prio),
                    raw_json=json.dumps({"detail": detail}),
                ))
        return run.run_id


def _banner(log, n, title):
    log("")
    log(f"── {n}. {title} " + "─" * max(2, 60 - len(title)))


def run_demo(output_dir, log=print):
    # 1. Record ----------------------------------------------------------------
    _banner(log, 1, "Record two runs of the agent")
    store = InMemoryTraceStore()
    golden = _record(store, "research-agent · main", GOLDEN_STEPS)
    candidate = _record(store, "research-agent · PR-42", CANDIDATE_STEPS)
    log(f"   golden:    {len(golden.events)} steps  {[e.payload.type_identifier for e in golden.events]}")
    log(f"   candidate: {len(candidate.events)} steps  {[e.payload.type_identifier for e in candidate.events]}")

    # 2. Query -----------------------------------------------------------------
    _banner(log, 2, "Query for a suspicious pattern (searched but never verified)")
    suspicious = store.query_runs(TraceQueryDSL().requiring_step("search").missing_step("verify"))
    log(f"   matched {len(suspicious)} run(s): {[r.context_id for r in suspicious]}")

    # 3. Gate ------------------------------------------------------------------
    _banner(log, 3, "Gate the candidate against the golden run")
    report = RegressionGate().check(golden, candidate)
    log(f"   verdict: {'PASS' if report.passed else 'REGRESSION'}  "
        f"(severity {report.regression_level.value}, strength {report.strength:.2f})")
    log(f"   removed critical steps: {report.removed_steps}")

    # 4. Anomaly rules ---------------------------------------------------------
    _banner(log, 4, "Run out-of-the-box anomaly rules over every recorded run")
    rules = [ToolDropRule("verify"), LoopingRule("search", max_repeats=3)]
    anomalies = AnomalyDetector(store).detect_anomalies(rules)
    for a in anomalies:
        log(f"   ! [{a.rule_name}] {a.description}")

    # 5. Diff ------------------------------------------------------------------
    _banner(log, 5, "Structural diff of the two runs")
    diff = TraceDiffEngine().diff(base=golden, comparison=candidate)
    for c in diff.changes:
        log(f"   {c.kind.value:<7} {c.type_identifier} ({c.engine_name}) @seq {c.original_sequence}")

    # 6. Report ----------------------------------------------------------------
    _banner(log, 6, "Export a shareable HTML report")
    html = render_report_html(
        report, golden_label="research-agent · main", candidate_label="research-agent · PR-42"
    )
    report_path = os.path.join(output_dir, "demo-report.html")
    with open(report_path, "w", encoding="utf-8") as fh:
        fh.write(html)
    log(f"   wrote {os.path.relpath(report_path)}  (open it, or Print → Save as PDF)")

    # 7. Hand off to the CLI + CI ----------------------------------------------
    _banner(log, 7, "Take the same runs to CI")
    db_path = os.path.join(output_dir, "demo-traces.sqlite")
    for ext in ("", "-wal", "-shm"):
        try:
            os.remove(db_path + ext)
        except OSError:
            pass
    sql = SQLiteTraceStore(AnyTraceableEvent, db_path, start_writer=False)
    gid = _record_erased(sql, "research-agent · main", GOLDEN_STEPS)
    cid = _record_erased(sql, "research-agent · PR-42", CANDIDATE_STEPS)
    sql.close()

    rel = os.path.relpath(db_path)
    log(f"   wrote {rel}  (golden={str(gid)[:8]}  candidate={str(cid)[:8]})")
    log("")
    log("   Gate it in CI (the same engine the GitHub Action / GitLab template wrap):")
    log(f"     dprovenancekit gate --db {rel} --golden {gid} --candidate {cid}")
    log("   Run the anomaly rules:")
    log(f"     dprovenancekit anomalies --db {rel} --rules examples/demo-rules.json")
    log("   List / select runs (baseline selection):")
    log(f"     dprovenancekit runs --db {rel} --latest --format id")
    log("   Visualize (span tree, payload inspector, side-by-side diff, report export):")
    log("     available in the hosted DProvenanceKit service (a separate commercial product)")

    # The whole arc must agree the candidate regressed — also makes this an end-to-end test.
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
        "db_path": db_path,
        "golden_id": str(gid),
        "candidate_id": str(cid),
    }


def main():
    out = os.environ.get("DPROV_DEMO_OUT") or os.path.dirname(os.path.abspath(__file__))
    print("DProvenanceKit — end-to-end demo")
    print("=" * 64)
    run_demo(out)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Punchy, colorized demo used to render assets/demo.gif (via demo/demo.tape).

Same real DProvenanceKit API as examples/end_to_end_demo.py, trimmed and paced for a
short screencast: record a healthy *golden* run and a *regressed* candidate, then watch
the regression gate catch the drift and block the PR.

    python demo/demo_gif.py
"""
from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC) and _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from dprovenancekit import (  # noqa: E402
    AnomalyDetector,
    DProvenanceKit,
    InMemoryTraceStore,
    LoopingRule,
    RegressionGate,
    ToolDropRule,
    TraceableEvent,
    TracePriority,
)

# ── ansi ──────────────────────────────────────────────────────────────────────
DIM, B = "\033[2m", "\033[1m"
GREEN, RED, YEL, CYAN, GREY = (
    "\033[38;5;42m", "\033[38;5;203m", "\033[38;5;179m", "\033[38;5;39m", "\033[38;5;245m",
)
R = "\033[0m"


def out(s: str = "", pause: float = 0.0) -> None:
    sys.stdout.write(s + "\n")
    sys.stdout.flush()
    if pause:
        time.sleep(pause)


# ── the agent's event model (one frozen dataclass — that's the whole setup) ─────
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


GOLDEN = [("Planner", "plan"), ("Retriever", "search"), ("Retriever", "rank"),
          ("Verifier", "verify"), ("Planner", "decide")]
# The regressed run looped its search tool and skipped verification.
CANDIDATE = [("Planner", "plan")] + [("Retriever", "search")] * 6 + [("Planner", "decide")]


def record(store, context_id, steps):
    kit = DProvenanceKit(AgentStep)
    with kit.run(context_id=context_id, store=store) as run:
        for engine, action in steps:
            with kit.with_engine(engine):
                kit.record(AgentStep(action))
        return store.get_run(run.run_id)


def main() -> None:
    store = InMemoryTraceStore()

    out(f"{B}{CYAN}DProvenanceKit{R}{B} — catch AI reasoning regressions before they ship{R}", 0.6)
    out(f"{GREY}record every run of your agent → query, diff, and gate it in CI{R}", 0.8)
    out()

    out(f"{DIM}$ python research_agent.py  {GREY}# record two runs of the same agent{R}", 0.5)
    golden = record(store, "research-agent · main", GOLDEN)
    candidate = record(store, "research-agent · PR-42", CANDIDATE)

    def line(label, run, color):
        parts, prev = [], None
        for e in run.events:
            a = e.payload.type_identifier
            if parts and parts[-1][0] == a:
                parts[-1][1] += 1
            else:
                parts.append([a, 1])
        chunks = [f"{a}{DIM}×{n}{R}{color}" if n > 1 else a for a, n in parts]
        arrow = f"{GREY} → {color}"
        return f"  {color}{label:<22}{R}{color}{arrow.join(chunks)}{R}"

    out(line("● golden  (main)", golden, GREEN), 0.5)
    out(line("● candidate (PR-42)", candidate, YEL), 1.0)
    out()

    out(f"{DIM}$ dprovenancekit gate --golden main --candidate PR-42{R}", 0.7)
    report = RegressionGate().check(golden, candidate)
    rules = [ToolDropRule("verify"), LoopingRule("search", max_repeats=3)]
    anomalies = AnomalyDetector(store).detect_anomalies(rules)

    verdict = "PASS" if report.passed else "REGRESSION"
    out(f"  {RED}{B}✗ {verdict}{R}  {GREY}severity {R}{RED}{report.regression_level.value.upper()}{R}"
        f"  {GREY}confidence {report.strength:.0%}{R}", 0.5)
    out(f"    {RED}▸{R} dropped critical step  {B}verify{R}  {GREY}(the agent stopped fact-checking){R}", 0.5)
    out(f"    {RED}▸{R} tool loop              {B}search ×6{R}  {GREY}(> 3 allowed){R}", 0.9)
    out()

    out(f"  {RED}exit 1 — pull request blocked.{R}  {GREY}the drift never reaches production.{R}", 0.4)
    out()
    out(f"{GREEN}✓{R} {GREY}pip install dprovenancekit{R}   {DIM}·  zero dependencies  ·  Apache-2.0{R}", 0.6)


if __name__ == "__main__":
    main()

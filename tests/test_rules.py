"""Tests for the out-of-the-box anomaly rule library (``dprovenancekit.rules``)."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from dprovenancekit import (
    AnomalyDetector,
    DProvenanceKit,
    InMemoryTraceStore,
    LoopingRule,
    ToolDropRule,
    TraceableEvent,
    TracePriority,
)


@dataclass(frozen=True)
class AgentStep(TraceableEvent):
    kind: str
    detail: str = ""

    @property
    def type_identifier(self) -> str:
        return self.kind

    @property
    def priority(self) -> TracePriority:
        return TracePriority.STRUCTURAL


def _record(store, context_id, steps):
    kit = DProvenanceKit(AgentStep)
    with kit.run(context_id=context_id, store=store) as run:
        for step in steps:
            kit.record(AgentStep(step))
        return run.run_id


def test_tool_drop_rule_flags_run_missing_required_step():
    store = InMemoryTraceStore()
    good = _record(store, "good", ["plan", "safety_check", "act"])
    dropped = _record(store, "dropped", ["plan", "act"])  # never ran safety_check

    anomalies = AnomalyDetector(store).detect_anomalies([ToolDropRule("safety_check")])

    flagged = {a.run_id for a in anomalies}
    assert dropped in flagged
    assert good not in flagged
    assert len(anomalies) == 1

    anomaly = anomalies[0]
    assert anomaly.rule_name == "tool_drop:safety_check"
    assert "safety_check" in anomaly.description


def test_tool_drop_rule_silent_when_every_run_has_the_step():
    store = InMemoryTraceStore()
    _record(store, "a", ["plan", "safety_check"])
    _record(store, "b", ["safety_check", "act"])

    anomalies = AnomalyDetector(store).detect_anomalies([ToolDropRule("safety_check")])
    assert anomalies == []


def test_tool_drop_rule_custom_name_and_dsl_query():
    rule = ToolDropRule("retrieve", name="missing_retrieval")
    assert rule.name == "missing_retrieval"
    assert rule.required_step == "retrieve"

    # The rule lowers to a single missing_step query usable directly against any backend.
    store = InMemoryTraceStore()
    bad = _record(store, "bad", ["answer"])
    _record(store, "ok", ["retrieve", "answer"])
    hits = store.query_runs(rule.anomaly_query)
    assert [r.run_id for r in hits] == [bad]


# ── LoopingRule ──────────────────────────────────────────────────────────────────


def test_looping_rule_flags_repeated_step():
    store = InMemoryTraceStore()
    looping = _record(store, "looping", ["call", "call", "call"])  # 3x call
    fine = _record(store, "fine", ["call", "done"])  # 1x call

    anomalies = AnomalyDetector(store).detect_anomalies([LoopingRule("call", max_repeats=2)])

    flagged = {a.run_id for a in anomalies}
    assert looping in flagged
    assert fine not in flagged
    assert len(anomalies) == 1

    anomaly = anomalies[0]
    assert anomaly.rule_name == "looping:call"
    assert "repeated 3 times" in anomaly.description


def test_looping_rule_threshold_is_strictly_more_than_max():
    store = InMemoryTraceStore()
    at_limit = _record(store, "at", ["call", "call"])  # exactly 2 — still healthy
    over = _record(store, "over", ["call", "call", "call"])  # 3 — looping

    flagged = {a.run_id for a in AnomalyDetector(store).detect_anomalies([LoopingRule("call", 2)])}
    assert over in flagged
    assert at_limit not in flagged


def test_looping_rule_validates_max_repeats():
    with pytest.raises(ValueError):
        LoopingRule("call", 0)

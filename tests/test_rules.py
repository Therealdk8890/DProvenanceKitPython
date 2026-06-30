"""Tests for the out-of-the-box anomaly rule library (``dprovenancekit.rules``)."""

from __future__ import annotations

from dataclasses import dataclass

from dprovenancekit import (
    AnomalyDetector,
    DProvenanceKit,
    InMemoryTraceStore,
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

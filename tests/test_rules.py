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
    build_rule,
    build_rules,
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


@pytest.mark.parametrize("bad", ["5", None, [5], True])
def test_looping_rule_rejects_non_int_max_repeats_with_valueerror(bad):
    # A typo'd config (e.g. quoting the number) must surface as ValueError, not a raw TypeError.
    with pytest.raises(ValueError):
        LoopingRule("call", bad)


@pytest.mark.parametrize("bad", [123, "", None, {"x": 1}])
def test_rules_reject_non_string_step(bad):
    with pytest.raises(ValueError):
        ToolDropRule(bad)
    with pytest.raises(ValueError):
        LoopingRule(bad, 2)


# ── registry (build_rule / build_rules) ──────────────────────────────────────────


def test_build_rule_constructs_known_types():
    drop = build_rule({"type": "tool_drop", "required_step": "safety_check"})
    assert isinstance(drop, ToolDropRule)
    assert drop.required_step == "safety_check"

    loop = build_rule({"type": "looping", "step": "web_search", "max_repeats": 5})
    assert isinstance(loop, LoopingRule)
    assert loop.step == "web_search" and loop.max_repeats == 5


def test_build_rule_honors_custom_name():
    rule = build_rule({"type": "tool_drop", "required_step": "x", "name": "custom"})
    assert rule.name == "custom"


def test_build_rule_rejects_unknown_type():
    with pytest.raises(ValueError, match="unknown rule type"):
        build_rule({"type": "nope"})


def test_build_rule_rejects_missing_field():
    with pytest.raises(ValueError, match="missing required field"):
        build_rule({"type": "looping", "step": "x"})  # no max_repeats


def test_build_rule_rejects_non_object_spec():
    with pytest.raises(ValueError, match="must be an object"):
        build_rule("tool_drop")


def test_build_rules_builds_a_list():
    rules = build_rules(
        [
            {"type": "tool_drop", "required_step": "a"},
            {"type": "looping", "step": "b", "max_repeats": 2},
        ]
    )
    assert [type(r).__name__ for r in rules] == ["ToolDropRule", "LoopingRule"]


def test_build_rule_surfaces_invalid_field_as_valueerror():
    # A quoted number is the most common misconfiguration; it must be a ValueError, not TypeError.
    with pytest.raises(ValueError):
        build_rule({"type": "looping", "step": "x", "max_repeats": "5"})
    with pytest.raises(ValueError):
        build_rule({"type": "tool_drop", "required_step": 123})

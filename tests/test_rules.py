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


def _record_with_engines(store, context_id, steps):
    """Record ``(kind, engine)`` pairs, each event under its engine (component) name."""
    kit = DProvenanceKit(AgentStep)
    with kit.run(context_id=context_id, store=store) as run:
        for kind, engine in steps:
            with kit.with_engine(engine):
                kit.record(AgentStep(kind))
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

    anomalies = AnomalyDetector(store).detect_anomalies(
        [LoopingRule("call", max_repeats=2)]
    )

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

    flagged = {
        a.run_id
        for a in AnomalyDetector(store).detect_anomalies([LoopingRule("call", 2)])
    }
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


def test_looping_rule_engine_scope_counts_only_that_engine():
    # Under the canonical vocabulary every tool call shares 'tool_call.start'; the
    # engine name is what distinguishes tools. engine='search' must count only the
    # search tool's calls, not the run's total tool-call volume.
    store = InMemoryTraceStore()
    hammering = _record_with_engines(
        store,
        "hammering",
        [("tool_call.start", "search")] * 3 + [("tool_call.start", "browse")],
    )
    diverse = _record_with_engines(
        store,
        "diverse",
        [
            ("tool_call.start", "search"),
            ("tool_call.start", "browse"),
            ("tool_call.start", "summarize"),
            ("tool_call.start", "verify"),
        ],
    )

    rule = LoopingRule("tool_call.start", 2, engine="search")
    assert rule.name == "looping:tool_call.start@search"
    assert rule.engine == "search"

    flagged = {a.run_id for a in AnomalyDetector(store).detect_anomalies([rule])}
    assert hammering in flagged  # search ran 3x > 2
    assert diverse not in flagged  # 4 total calls, but search only once


def test_looping_rule_per_engine_flags_any_single_hot_engine():
    store = InMemoryTraceStore()
    stuck = _record_with_engines(
        store,
        "stuck",
        [("tool_call.start", "flaky_api")] * 3 + [("tool_call.start", "search")],
    )
    # Same total volume (4 calls > max_repeats), but spread evenly — the old
    # total-count semantics false-positived on exactly this shape.
    spread = _record_with_engines(
        store,
        "spread",
        [("tool_call.start", "search")] * 2 + [("tool_call.start", "browse")] * 2,
    )

    rule = LoopingRule("tool_call.start", 2, per_engine=True)
    assert rule.name == "looping:tool_call.start:per-engine"
    assert rule.per_engine is True

    anomalies = AnomalyDetector(store).detect_anomalies([rule])
    flagged = {a.run_id for a in anomalies}
    assert stuck in flagged
    assert spread not in flagged
    (anomaly,) = anomalies
    assert "flaky_api" in anomaly.description  # names the hot engine


def test_looping_rule_default_remains_total_count():
    # Backward compatibility: the positional two-arg form is still a total budget
    # across engines.
    store = InMemoryTraceStore()
    spread = _record_with_engines(
        store,
        "spread",
        [("tool_call.start", "search")] * 2 + [("tool_call.start", "browse")] * 2,
    )
    flagged = {
        a.run_id
        for a in AnomalyDetector(store).detect_anomalies(
            [LoopingRule("tool_call.start", 3)]
        )
    }
    assert spread in flagged  # 4 total > 3, regardless of engine spread


def test_looping_rule_engine_and_per_engine_are_mutually_exclusive():
    with pytest.raises(ValueError):
        LoopingRule("tool_call.start", 2, engine="search", per_engine=True)


@pytest.mark.parametrize("bad", ["", 123, ["search"]])
def test_looping_rule_rejects_invalid_engine(bad):
    with pytest.raises(ValueError):
        LoopingRule("tool_call.start", 2, engine=bad)


@pytest.mark.parametrize("bad", ["true", 1, None])
def test_looping_rule_rejects_non_bool_per_engine(bad):
    with pytest.raises(ValueError):
        LoopingRule("tool_call.start", 2, per_engine=bad)


def test_build_rule_constructs_engine_scoped_and_per_engine_looping():
    scoped = build_rule(
        {"type": "looping", "step": "tool_call.start", "max_repeats": 5, "engine": "search"}
    )
    assert scoped.engine == "search"
    assert scoped.per_engine is False

    grouped = build_rule(
        {"type": "looping", "step": "tool_call.start", "max_repeats": 5, "per_engine": True}
    )
    assert grouped.per_engine is True
    assert grouped.engine is None

    with pytest.raises(ValueError):
        build_rule(
            {
                "type": "looping",
                "step": "tool_call.start",
                "max_repeats": 5,
                "per_engine": "true",  # quoted bool — common JSON misconfiguration
            }
        )


@pytest.mark.parametrize("bad", [123, "", None, {"x": 1}])
def test_rules_reject_non_string_step(bad):
    with pytest.raises(ValueError):
        ToolDropRule(bad)
    with pytest.raises(ValueError):
        LoopingRule(bad, 2)


# ── UnregisteredToolRule ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ToolCallStep(TraceableEvent):
    """A typed event carrying a tool name and the registry it should belong to.

    Its default ``to_dict()`` reflects these fields — importantly it has NO
    ``raw_json`` attribute, so it exercises the typed-store path the rule must handle.
    """

    name: str
    registered_tools: tuple = ()

    @property
    def type_identifier(self) -> str:
        return "tool_call"

    @property
    def priority(self) -> TracePriority:
        return TracePriority.STRUCTURAL


def _record_tool_calls(store, context_id, calls):
    kit = DProvenanceKit(ToolCallStep)
    with kit.run(context_id=context_id, store=store) as run:
        for name, registry in calls:
            kit.record(ToolCallStep(name=name, registered_tools=tuple(registry)))
        return run.run_id


def test_unregistered_tool_rule_flags_call_outside_registry():
    from dprovenancekit.rules import UnregisteredToolRule

    store = InMemoryTraceStore()
    registry = ["search", "verify"]
    rogue = _record_tool_calls(store, "rogue", [("search", registry), ("exfiltrate", registry)])
    clean = _record_tool_calls(store, "clean", [("search", registry), ("verify", registry)])

    rule = UnregisteredToolRule("tool_call", "registered_tools")
    anomalies = AnomalyDetector(store).detect_anomalies([rule])

    flagged = {a.run_id for a in anomalies}
    # The key regression: typed events (no raw_json) must still be inspected via
    # to_dict(). The old raw_json read silently returned False here.
    assert rogue in flagged
    assert clean not in flagged
    assert anomalies[0].rule_name == "unregistered_tool:tool_call"


def test_unregistered_tool_rule_silent_when_all_calls_registered():
    from dprovenancekit.rules import UnregisteredToolRule

    store = InMemoryTraceStore()
    _record_tool_calls(store, "ok", [("search", ["search", "verify"])])
    rule = UnregisteredToolRule("tool_call", "registered_tools")
    assert AnomalyDetector(store).detect_anomalies([rule]) == []


def test_unregistered_tool_rule_validates_args():
    from dprovenancekit.rules import UnregisteredToolRule

    with pytest.raises(ValueError):
        UnregisteredToolRule("", "registered_tools")
    with pytest.raises(ValueError):
        UnregisteredToolRule("tool_call", "")


def test_build_rule_constructs_unregistered_tool():
    from dprovenancekit.rules import UnregisteredToolRule

    rule = build_rule(
        {"type": "unregistered_tool", "step": "tool_call", "registry_field": "registered_tools"}
    )
    assert isinstance(rule, UnregisteredToolRule)
    assert rule.name == "unregistered_tool:tool_call"


# ── UnusedToolResultRule ─────────────────────────────────────────────────────────


def test_unused_tool_result_rule_flags_orphan_result():
    from dprovenancekit.rules import UnusedToolResultRule

    store = InMemoryTraceStore()
    orphan = _record(store, "orphan", ["tool_result"])  # never used
    trailing = _record(store, "trailing", ["tool_result", "respond", "tool_result"])
    used = _record(store, "used", ["tool_result", "respond"])

    rule = UnusedToolResultRule("tool_result", "respond")
    flagged = {a.run_id for a in AnomalyDetector(store).detect_anomalies([rule])}
    assert orphan in flagged
    assert trailing in flagged  # second result has no follow-up before end
    assert used not in flagged


def test_unused_tool_result_rule_silent_when_each_result_used():
    from dprovenancekit.rules import UnusedToolResultRule

    store = InMemoryTraceStore()
    _record(store, "ok", ["tool_result", "respond", "tool_result", "respond"])
    rule = UnusedToolResultRule("tool_result", "respond")
    assert AnomalyDetector(store).detect_anomalies([rule]) == []


def test_unused_tool_result_rule_parallel_fanout_is_healthy():
    # Frameworks fan out tool calls: several results land before the next model step,
    # and the model then sees all of them. That shape must not be flagged (the old
    # strictly-serial semantics false-positived on every such run).
    from dprovenancekit.rules import UnusedToolResultRule

    store = InMemoryTraceStore()
    fanout = _record(
        store,
        "fanout",
        ["tool_result", "tool_result", "tool_result", "respond"],
    )
    serial = _record(store, "serial", ["tool_result", "respond", "tool_result", "respond"])
    trailing = _record(
        store,
        "trailing",
        ["tool_result", "tool_result", "respond", "tool_result"],  # last one unused
    )

    rule = UnusedToolResultRule("tool_result", "respond")
    flagged = {a.run_id for a in AnomalyDetector(store).detect_anomalies([rule])}
    assert fanout not in flagged
    assert serial not in flagged
    assert trailing in flagged


def test_unused_tool_result_rule_describe_counts_outstanding_results():
    from dprovenancekit.rules import UnusedToolResultRule

    store = InMemoryTraceStore()
    _record(store, "two-orphans", ["respond", "tool_result", "tool_result"])
    rule = UnusedToolResultRule("tool_result", "respond")
    (anomaly,) = AnomalyDetector(store).detect_anomalies([rule])
    assert "2 result(s)" in anomaly.description


def test_unused_tool_result_rule_validates_args():
    from dprovenancekit.rules import UnusedToolResultRule

    with pytest.raises(ValueError):
        UnusedToolResultRule("", "respond")
    with pytest.raises(ValueError):
        UnusedToolResultRule("tool_result", "")


def test_build_rule_constructs_unused_tool_result():
    from dprovenancekit.rules import UnusedToolResultRule

    rule = build_rule(
        {
            "type": "unused_tool_result",
            "step": "tool_result",
            "required_followup_step": "reasoning_or_response",
        }
    )
    assert isinstance(rule, UnusedToolResultRule)
    assert rule.name == "unused_tool_result:tool_result"


def test_build_rule_carries_id_severity_and_message_onto_anomaly():
    store = InMemoryTraceStore()
    dropped = _record(store, "dropped", ["plan", "act"])  # missing safety_check

    rule = build_rule(
        {
            "id": "agent.safety",
            "type": "tool_drop",
            "required_step": "safety_check",
            "severity": "error",
            "message": "safety check skipped",
        }
    )
    assert rule.name == "agent.safety"  # id used as the name
    assert rule.severity == "error"

    (anomaly,) = AnomalyDetector(store).detect_anomalies([rule])
    assert anomaly.run_id == dropped
    assert anomaly.severity == "error"
    assert anomaly.message == "safety check skipped"


def test_build_rule_defaults_severity_when_absent():
    rule = build_rule({"type": "looping", "step": "x", "max_repeats": 2})
    assert rule.severity == "warning"
    assert rule.message is None


def test_bundled_agent_preset_loads_and_fires_on_normalized_events():
    # The shipped preset must load and its rules must fire against the vendor-neutral
    # event names it targets (tool_call.start/.end, llm_call.start — what OTel ingestion
    # normalizes traces to). Uses synthetic events so it holds regardless of which
    # branch produces those names.
    import json
    from importlib.resources import files

    config = json.loads(
        files("dprovenancekit").joinpath("rulesets/agent.json").read_text(encoding="utf-8")
    )
    rules = build_rules(config["rules"])
    assert {r.name for r in rules} == {
        "agent.runaway_tool_use",
        "agent.unused_tool_result",
    }

    store = InMemoryTraceStore()
    # Every tool call shares 'tool_call.start'; the tool identity is the engine name.
    runaway = _record_with_engines(
        store, "runaway", [("tool_call.start", "flaky_api")] * 11  # one tool, 11 calls
    )
    unused = _record(store, "unused", ["tool_call.start", "tool_call.end"])  # no llm follow-up
    healthy = _record(
        store, "healthy", ["tool_call.start", "tool_call.end", "llm_call.start"]
    )
    # A busy-but-healthy research agent: 12 tool calls across 12 DISTINCT tools, each
    # result consumed. The preset must not treat sheer volume as a loop.
    busy = _record_with_engines(
        store,
        "busy",
        [("tool_call.start", f"tool_{i}") for i in range(12)]
        + [("tool_call.end", f"tool_{i}") for i in range(12)]
        + [("llm_call.start", "gpt-4o")],
    )

    anomalies = AnomalyDetector(store).detect_anomalies(rules)
    flagged = {(a.run_id, a.rule_name) for a in anomalies}
    assert (runaway, "agent.runaway_tool_use") in flagged
    assert (unused, "agent.unused_tool_result") in flagged
    assert healthy not in {a.run_id for a in anomalies}
    assert busy not in {a.run_id for a in anomalies}


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

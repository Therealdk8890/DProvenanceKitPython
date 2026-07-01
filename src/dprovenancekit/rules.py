"""Out-of-the-box anomaly rule templates.

The anomaly framework (:mod:`dprovenancekit.anomaly`) ships the :class:`AnomalyRule`
abstraction but no concrete rules. This module is the drop-in library: ready-made rules a
team can register with an :class:`~dprovenancekit.anomaly.AnomalyDetector` without writing
the query DSL by hand.

    from dprovenancekit import AnomalyDetector, ToolDropRule

    # Flag any run that never performed the safety check.
    anomalies = AnomalyDetector(store).detect_anomalies([ToolDropRule("safety_check")])

Each rule lowers to a :class:`~dprovenancekit.query.TraceQueryDSL` query, so it works against
every store backend and in the live engine exactly like a hand-written rule.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from .anomaly import AnomalyRule
from .query import TraceQueryDSL, TraceRun


class ToolDropRule(AnomalyRule):
    """Flag runs that never performed a required step.

    *Tool drop* — an agent stops calling a tool it is supposed to call. This is the
    single-run, absolute form: a run is anomalous iff ``required_step`` never appears in it.
    It needs no baseline and is expressible directly in the query DSL today
    (``missing_step``).

    The baseline-relative framing ("the tool was present in the golden run but dropped in
    this one") is a *diff* concern, not a single-run query — use
    :class:`~dprovenancekit.testing.RegressionGate` (or
    :class:`~dprovenancekit.diff.TraceDiffEngine`) for that, because
    :attr:`~dprovenancekit.anomaly.AnomalyRule.anomaly_query` evaluates one run in isolation
    and cannot reference another.

    Args:
        required_step: the ``type_identifier`` of the step/tool that must appear in the run.
        name: optional rule-name override (default ``"tool_drop:<required_step>"``).
    """

    def __init__(self, required_step: str, *, name: Optional[str] = None) -> None:
        if not isinstance(required_step, str) or not required_step:
            raise ValueError("required_step must be a non-empty string")
        self._required_step = required_step
        self._name = name or f"tool_drop:{required_step}"

    @property
    def required_step(self) -> str:
        return self._required_step

    @property
    def name(self) -> str:
        return self._name

    @property
    def anomaly_query(self) -> TraceQueryDSL:
        return TraceQueryDSL().missing_step(self._required_step)

    def describe(self, run: TraceRun) -> str:
        return (
            f"required step '{self._required_step}' was never recorded "
            f"(context '{run.context_id}', run {run.run_id})"
        )


class LoopingRule(AnomalyRule):
    """Flag runs where a step repeats more than a threshold — an agent stuck in a loop.

    A run is anomalous iff ``step`` occurs **more than** ``max_repeats`` times (i.e. at least
    ``max_repeats + 1``). Use it to catch an agent that calls the same tool over and over.

    Args:
        step: the ``type_identifier`` of the repeating step/tool.
        max_repeats: the largest number of occurrences still considered healthy (>= 1).
        name: optional rule-name override (default ``"looping:<step>"``).
    """

    def __init__(self, step: str, max_repeats: int, *, name: Optional[str] = None) -> None:
        if not isinstance(step, str) or not step:
            raise ValueError("step must be a non-empty string")
        if not isinstance(max_repeats, int) or isinstance(max_repeats, bool) or max_repeats < 1:
            raise ValueError("LoopingRule.max_repeats must be an int >= 1")
        self._step = step
        self._max_repeats = max_repeats
        self._name = name or f"looping:{step}"

    @property
    def step(self) -> str:
        return self._step

    @property
    def max_repeats(self) -> int:
        return self._max_repeats

    @property
    def name(self) -> str:
        return self._name

    @property
    def anomaly_query(self) -> TraceQueryDSL:
        return TraceQueryDSL().requiring_repeated_step(self._step, self._max_repeats + 1)

    def describe(self, run: TraceRun) -> str:
        seen = sum(1 for e in run.events if e.payload.type_identifier == self._step)
        return (
            f"step '{self._step}' repeated {seen} times (> {self._max_repeats} allowed) "
            f"in run {run.run_id} (context '{run.context_id}')"
        )


# MARK: - Registry --------------------------------------------------------------
#
# Maps a ``type`` string to a builder that constructs the rule from a plain dict spec, so a
# team can declare rules in a JSON/YAML config (e.g. for CI) instead of writing Python:
#
#     {"rules": [
#         {"type": "tool_drop", "required_step": "safety_check"},
#         {"type": "looping", "step": "web_search", "max_repeats": 5}
#     ]}

_RULE_BUILDERS = {
    "tool_drop": lambda s: ToolDropRule(s["required_step"], name=s.get("name")),
    "looping": lambda s: LoopingRule(s["step"], s["max_repeats"], name=s.get("name")),
}


def build_rule(spec: Dict[str, Any]) -> AnomalyRule:
    """Construct an :class:`AnomalyRule` from a plain dict spec (e.g. parsed from JSON).

    Raises :class:`ValueError` for a missing/unknown ``type`` or a missing required field.
    """
    try:
        rule_type = spec["type"]
    except (KeyError, TypeError):
        raise ValueError("rule spec must be an object with a 'type' field")
    builder = _RULE_BUILDERS.get(rule_type)
    if builder is None:
        raise ValueError(
            f"unknown rule type {rule_type!r}; known types: {sorted(_RULE_BUILDERS)}"
        )
    try:
        return builder(spec)
    except KeyError as exc:
        raise ValueError(f"rule {rule_type!r} is missing required field {exc}")


def build_rules(specs: Iterable[Dict[str, Any]]) -> List[AnomalyRule]:
    """Construct a list of rules from an iterable of dict specs."""
    return [build_rule(spec) for spec in specs]


__all__ = ["ToolDropRule", "LoopingRule", "build_rule", "build_rules"]


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

    Three scopes, because ``type_identifier`` alone is often too coarse: under the
    canonical vocabulary *every* tool call shares ``tool_call.start``, so a raw count is
    a total tool-call budget, not loop detection. The engine (component name — the tool
    or model identity stamped on each event) is what distinguishes one tool from
    another:

    * **Total** (default, ``LoopingRule("tool_call.start", 10)``): anomalous iff
      ``step`` occurs more than ``max_repeats`` times in the run, regardless of engine.
      An overall step budget.
    * **One engine** (``engine="search"``): anomalous iff the ``search`` engine
      specifically emitted ``step`` more than ``max_repeats`` times. Lowers entirely to
      the query DSL (engine-scoped count), so it runs on every backend.
    * **Any engine** (``per_engine=True``): anomalous iff *some single* engine emitted
      ``step`` more than ``max_repeats`` times — the "agent stuck hammering one tool"
      semantic, without naming the tool up front. The DSL cannot group by engine, so
      this lowers to the total count as a sound pre-filter (any single engine over the
      threshold forces the total over it) and refines per engine in
      :meth:`is_anomalous` — the standard confirm hook every backend already applies.

    ``engine`` and ``per_engine`` are mutually exclusive.

    Args:
        step: the ``type_identifier`` of the repeating step/tool.
        max_repeats: the largest number of occurrences still considered healthy (>= 1).
        engine: optional engine (component) name to scope the count to.
        per_engine: flag if any single engine exceeds ``max_repeats`` occurrences.
        name: optional rule-name override (default ``"looping:<step>"``, with
            ``"@<engine>"`` appended when engine-scoped, ``":per-engine"`` when
            ``per_engine``).
    """

    def __init__(
        self,
        step: str,
        max_repeats: int,
        *,
        engine: Optional[str] = None,
        per_engine: bool = False,
        name: Optional[str] = None,
    ) -> None:
        if not isinstance(step, str) or not step:
            raise ValueError("step must be a non-empty string")
        if (
            not isinstance(max_repeats, int)
            or isinstance(max_repeats, bool)
            or max_repeats < 1
        ):
            raise ValueError("LoopingRule.max_repeats must be an int >= 1")
        if engine is not None and (not isinstance(engine, str) or not engine):
            raise ValueError("LoopingRule.engine must be a non-empty string when given")
        if not isinstance(per_engine, bool):
            raise ValueError("LoopingRule.per_engine must be a bool")
        if engine is not None and per_engine:
            raise ValueError(
                "LoopingRule.engine and per_engine are mutually exclusive: "
                "engine scopes to one named engine, per_engine flags any engine"
            )
        self._step = step
        self._max_repeats = max_repeats
        self._engine = engine
        self._per_engine = per_engine
        if name:
            self._name = name
        elif engine is not None:
            self._name = f"looping:{step}@{engine}"
        elif per_engine:
            self._name = f"looping:{step}:per-engine"
        else:
            self._name = f"looping:{step}"

    @property
    def step(self) -> str:
        return self._step

    @property
    def max_repeats(self) -> int:
        return self._max_repeats

    @property
    def engine(self) -> Optional[str]:
        return self._engine

    @property
    def per_engine(self) -> bool:
        return self._per_engine

    @property
    def name(self) -> str:
        return self._name

    @property
    def anomaly_query(self) -> TraceQueryDSL:
        # per_engine: the total count is a sound pre-filter (a single engine over the
        # threshold forces the total over it); is_anomalous performs the per-engine
        # grouping the DSL cannot express.
        return TraceQueryDSL().requiring_repeated_step(
            self._step, self._max_repeats + 1, engine=self._engine
        )

    def is_anomalous(self, run: TraceRun) -> bool:
        if not self._per_engine:
            return True
        return any(
            count > self._max_repeats for count in self._counts_by_engine(run).values()
        )

    def _counts_by_engine(self, run: TraceRun) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for e in run.events:
            if e.payload.type_identifier == self._step:
                counts[e.engine_name] = counts.get(e.engine_name, 0) + 1
        return counts

    def describe(self, run: TraceRun) -> str:
        where = f"in run {run.run_id} (context '{run.context_id}')"
        if self._engine is not None:
            seen = self._counts_by_engine(run).get(self._engine, 0)
            return (
                f"engine '{self._engine}' repeated step '{self._step}' {seen} times "
                f"(> {self._max_repeats} allowed) {where}"
            )
        if self._per_engine:
            counts = self._counts_by_engine(run)
            worst, seen = max(counts.items(), key=lambda kv: kv[1], default=("?", 0))
            return (
                f"engine '{worst}' repeated step '{self._step}' {seen} times "
                f"(> {self._max_repeats} allowed per engine) {where}"
            )
        seen = sum(1 for e in run.events if e.payload.type_identifier == self._step)
        return (
            f"step '{self._step}' repeated {seen} times (> {self._max_repeats} allowed) "
            f"{where}"
        )


class UnregisteredToolRule(AnomalyRule):
    """Flag runs where an agent calls a tool not in the registry field.
    
    Args:
        step: the ``type_identifier`` of the tool call step.
        registry_field: the payload field containing the list of registered tools.
        name: optional rule-name override.
    """

    def __init__(self, step: str, registry_field: str, *, name: Optional[str] = None) -> None:
        if not isinstance(step, str) or not step:
            raise ValueError("step must be a non-empty string")
        if not isinstance(registry_field, str) or not registry_field:
            raise ValueError("registry_field must be a non-empty string")
        self._step = step
        self._registry_field = registry_field
        self._name = name or f"unregistered_tool:{step}"

    @property
    def name(self) -> str:
        return self._name

    @property
    def anomaly_query(self) -> TraceQueryDSL:
        return TraceQueryDSL().requiring_step(self._step)

    def is_anomalous(self, run: TraceRun) -> bool:
        # Use to_dict(), implemented by every TraceableEvent, so the rule works on
        # typed event stores (OpenAIAgentsTraceEvent, LangChainTraceEvent, user
        # dataclasses) as well as the type-erased AnyTraceableEvent — reading raw_json
        # directly would AttributeError (and silently return False) on typed payloads.
        for e in run.events:
            if e.payload.type_identifier != self._step:
                continue
            try:
                payload = e.payload.to_dict()
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            tool_name = payload.get("tool_name") or payload.get("name")
            registry = payload.get(self._registry_field) or []
            if tool_name and tool_name not in registry:
                return True
        return False

    def describe(self, run: TraceRun) -> str:
        return f"unregistered tool called in step '{self._step}' (not in '{self._registry_field}')"


class UnusedToolResultRule(AnomalyRule):
    """Flag runs where a tool result is produced but never used downstream.

    *Unused tool result* — an agent receives a tool result and then finishes without a
    reasoning/response step consuming it. The semantic is parallel-safe: frameworks
    routinely fan out several tool calls whose results all land before the next model
    step, and the model then sees everything produced so far. So a
    ``required_followup_step`` occurrence consumes **all** ``step`` results outstanding
    at that point, and a run is anomalous iff one or more results are still outstanding
    when the run ends (i.e. some ``step`` occurrence is never followed — at any
    distance — by a ``required_followup_step``).

    Args:
        step: the ``type_identifier`` of the tool-result step.
        required_followup_step: the ``type_identifier`` that must follow it (e.g. a
            reasoning or response step).
        name: optional rule-name override (default ``"unused_tool_result:<step>"``).
    """

    def __init__(
        self, step: str, required_followup_step: str, *, name: Optional[str] = None
    ) -> None:
        if not isinstance(step, str) or not step:
            raise ValueError("step must be a non-empty string")
        if not isinstance(required_followup_step, str) or not required_followup_step:
            raise ValueError("required_followup_step must be a non-empty string")
        self._step = step
        self._followup = required_followup_step
        self._name = name or f"unused_tool_result:{step}"

    @property
    def name(self) -> str:
        return self._name

    @property
    def anomaly_query(self) -> TraceQueryDSL:
        # Pre-filter to runs that contain the tool-result step; is_anomalous refines.
        return TraceQueryDSL().requiring_step(self._step)

    def is_anomalous(self, run: TraceRun) -> bool:
        # Count of tool results not yet consumed. A followup consumes ALL outstanding
        # results (the model sees everything produced so far), which keeps parallel
        # fan-out — several results landing before the next model step — healthy.
        return self._outstanding(run) > 0

    def _outstanding(self, run: TraceRun) -> int:
        outstanding = 0
        for e in sorted(run.events, key=lambda ev: ev.sequence):
            kind = e.payload.type_identifier
            if kind == self._step:
                outstanding += 1
            elif kind == self._followup:
                outstanding = 0
        return outstanding

    def describe(self, run: TraceRun) -> str:
        outstanding = self._outstanding(run)
        return (
            f"{outstanding} result(s) of '{self._step}' arrived after the last "
            f"'{self._followup}' and were never consumed "
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

# Each builder takes the spec dict. ``name`` falls back to ``id`` so a ruleset can label
# rules with either key.
def _rule_name(s: Dict[str, Any]) -> Optional[str]:
    return s.get("name") or s.get("id")


_RULE_BUILDERS = {
    "tool_drop": lambda s: ToolDropRule(s["required_step"], name=_rule_name(s)),
    "looping": lambda s: LoopingRule(
        s["step"],
        s["max_repeats"],
        engine=s.get("engine"),
        per_engine=s.get("per_engine", False),
        name=_rule_name(s),
    ),
    "unregistered_tool": lambda s: UnregisteredToolRule(
        s["step"], s["registry_field"], name=_rule_name(s)
    ),
    "unused_tool_result": lambda s: UnusedToolResultRule(
        s["step"], s["required_followup_step"], name=_rule_name(s)
    ),
}


def build_rule(spec: Dict[str, Any]) -> AnomalyRule:
    """Construct an :class:`AnomalyRule` from a plain dict spec (e.g. parsed from JSON).

    Recognizes the optional presentation fields ``severity`` and ``message`` and carries
    them onto the rule (surfaced on each :class:`~dprovenancekit.anomaly.Anomaly`).

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
        rule = builder(spec)
    except KeyError as exc:
        raise ValueError(f"rule {rule_type!r} is missing required field {exc}")
    if isinstance(spec, dict):
        if spec.get("severity"):
            rule.severity = str(spec["severity"])
        if spec.get("message"):
            rule.message = str(spec["message"])
    return rule


def build_rules(specs: Iterable[Dict[str, Any]]) -> List[AnomalyRule]:
    """Construct a list of rules from an iterable of dict specs."""
    return [build_rule(spec) for spec in specs]


__all__ = [
    "ToolDropRule",
    "LoopingRule",
    "UnregisteredToolRule",
    "UnusedToolResultRule",
    "build_rule",
    "build_rules",
]

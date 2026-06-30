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

from typing import Optional

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


__all__ = ["ToolDropRule"]

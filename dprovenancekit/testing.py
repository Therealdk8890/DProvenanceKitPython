"""A drop-in regression gate for tests and CI.

Turns "did my agent regress?" into a single assertion. Given a *golden* run (a known-good
trace) and a *candidate* run (the trace your current code produced), the gate aligns them
and decides pass/fail against a policy you choose, with a readable diagnostic on failure.

    from dprovenancekit.testing import assert_no_regression

    # in a pytest test:
    assert_no_regression(golden=golden_run, candidate=candidate_run)

By default the gate is **strict**: the candidate must align to the golden with every step
an exact match — any removed, added, or changed (ambiguous) step fails, and a removed
CRITICAL step is additionally a HIGH-severity regression. Loosen it with
``max_regression_level`` (gate only on severity) and ``allow_divergent_steps`` (tolerate
benign per-step changes), or pass your own ``evaluator`` to define what "equivalent" means.

Detecting *reordered* steps (the same steps in a different order) requires a span-aware
profile such as :attr:`AlignmentProfile.developer_debug_v1`. Under the default linear
profile a pure reorder binds 1:1 and reads as still-matching, so reordering alone does not
fail the default gate — pass a span-aware profile if order is part of what you're guarding.

This complements :class:`~dprovenancekit.alignment_snapshot.AlignmentSnapshotValidator`
(an exact output-hash snapshot of one alignment): the gate operates on two *runs*, reasons
about regression *severity*, and is built to read well inside a test failure.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Dict, List, Optional

from .alignment_config import (
    AlignmentConfiguration,
    AlignmentProfile,
    AnyEquivalenceEvaluator,
)
from .alignment_engine import TraceAlignmentEngine
from .alignment_models import AlignmentStateKind, RegressionLevel
from .priority import TracePriority
from .query import TraceRun

# RegressionLevel's raw values are strings, so define the severity ordering explicitly.
_LEVEL_ORDER: Dict[RegressionLevel, int] = {
    RegressionLevel.NONE: 0,
    RegressionLevel.LOW: 1,
    RegressionLevel.MEDIUM: 2,
    RegressionLevel.HIGH: 3,
}

# Per-step states that mean the candidate diverged from the golden. ``semanticMatch`` is
# deliberately excluded: it means the evaluator judged a changed payload still equivalent,
# which is the whole point of supplying a custom evaluator. ``exactMatch`` is unchanged.
_DIVERGENT_KINDS = frozenset(
    {
        AlignmentStateKind.REMOVED,
        AlignmentStateKind.ADDED,
        AlignmentStateKind.REORDERED,
        AlignmentStateKind.AMBIGUOUS,
    }
)

# The same kinds in a fixed order, for any user-facing list that must read the same across
# processes — ``frozenset`` iteration order is not stable (it depends on PYTHONHASHSEED).
_DIVERGENT_KINDS_ORDERED = (
    AlignmentStateKind.REMOVED,
    AlignmentStateKind.ADDED,
    AlignmentStateKind.REORDERED,
    AlignmentStateKind.AMBIGUOUS,
)


def run_fingerprint(run: TraceRun) -> str:
    """The run's structural identity (Trace Spec v1 §5): SHA-1 over each event's
    ``type:engine|`` signature in sequence order. Equal fingerprints ⇒ same typed steps
    through the same engines in the same order. A fast pre-check before full alignment.
    """
    digest = hashlib.sha1()
    for event in sorted(run.events, key=lambda e: e.sequence):
        digest.update(
            f"{event.payload.type_identifier}:{event.engine_name or ''}|".encode(
                "utf-8"
            )
        )
    return digest.hexdigest()


def exact_equality_evaluator() -> AnyEquivalenceEvaluator:
    """The default evaluator: two steps are equivalent iff their payloads are fully equal.

    Predictable and dependency-free — the same canonical evaluator the conformance suite
    uses. Supply your own to ignore volatile fields (timestamps, token counts, latencies).
    """
    return AnyEquivalenceEvaluator(
        evaluator_identifier="ExactEquality_v1",
        evaluator=lambda a, b: 1.0 if a == b else 0.0,
    )


@dataclass(frozen=True)
class RegressionReport:
    """The outcome of a gate check. ``passed`` is the verdict; the rest explains it."""

    passed: bool
    regression_level: RegressionLevel
    strength: float
    reasoning: str
    fingerprint_match: bool
    golden_fingerprint: str
    candidate_fingerprint: str
    steps_by_change: Dict[str, List[str]]  # state kind value -> step type_identifiers
    max_regression_level: RegressionLevel
    allow_divergent_steps: bool

    @property
    def removed_steps(self) -> List[str]:
        return self.steps_by_change.get(AlignmentStateKind.REMOVED.value, [])

    @property
    def added_steps(self) -> List[str]:
        return self.steps_by_change.get(AlignmentStateKind.ADDED.value, [])

    @property
    def divergent_steps(self) -> List[str]:
        """All steps that diverged in a way the strict gate fails on (removed/added/
        reordered/ambiguous), flattened in a fixed, reproducible order."""
        out: List[str] = []
        for kind in _DIVERGENT_KINDS_ORDERED:
            out.extend(self.steps_by_change.get(kind.value, []))
        return out

    def summary(self) -> str:
        lines = [
            f"Regression gate: {'PASS' if self.passed else 'FAIL'}",
            f"  severity: {self.regression_level.value} (strength {self.strength:.2f}); "
            f"max allowed: {self.max_regression_level.value}",
            f"  fingerprint: {'match' if self.fingerprint_match else 'differs'} "
            f"({self.golden_fingerprint[:12]}… vs {self.candidate_fingerprint[:12]}…)",
        ]
        semantic_key = AlignmentStateKind.SEMANTIC_MATCH.value
        divergent = {k: v for k, v in self.steps_by_change.items() if k != semantic_key}
        accepted = self.steps_by_change.get(semantic_key, [])
        if divergent:
            lines.append("  per-step changes:")
            for kind_value in sorted(divergent):
                lines.append(f"    {kind_value}: {', '.join(divergent[kind_value])}")
        else:
            lines.append("  per-step changes: none (all exact matches)")
        if accepted:
            # semanticMatch is an evaluator-accepted equivalence, not a divergence.
            lines.append(
                f"  accepted as equivalent (semanticMatch): {', '.join(accepted)}"
            )
        if self.reasoning:
            lines.append(f"  engine: {self.reasoning}")
        return "\n".join(lines)

    def __str__(self) -> str:  # pragma: no cover - thin delegation
        return self.summary()


class RegressionError(AssertionError):
    """Raised when a gate check fails. Subclasses ``AssertionError`` so test runners treat
    it as an ordinary failed assertion. Carries the full :class:`RegressionReport`."""

    def __init__(self, report: RegressionReport):
        super().__init__(report.summary())
        self.report = report


class RegressionGate:
    """A configured regression policy you can reuse across many golden/candidate pairs.

    Args:
        profile: the alignment profile (default :attr:`AlignmentProfile.strict_audit_v1`).
        evaluator: payload equivalence evaluator (default :func:`exact_equality_evaluator`).
        minimum_priority: events below this tier are ignored (default ``STRUCTURAL`` —
            telemetry/diagnostic noise never affects the verdict).
        max_regression_level: the worst engine-assessed severity that still passes
            (default ``NONE``; raise to ``HIGH`` to disable the severity gate entirely).
            The engine currently assigns only ``NONE`` or ``HIGH`` (a removed CRITICAL step
            is ``HIGH``), so ``LOW``/``MEDIUM`` behave identically to ``NONE`` today.
        allow_divergent_steps: if ``False`` (default), any removed/added/reordered/
            ambiguous step fails the gate. If ``True``, only ``max_regression_level``
            decides — benign per-step changes are tolerated.
    """

    def __init__(
        self,
        *,
        profile: Optional[AlignmentProfile] = None,
        evaluator: Optional[AnyEquivalenceEvaluator] = None,
        minimum_priority: TracePriority = TracePriority.STRUCTURAL,
        max_regression_level: RegressionLevel = RegressionLevel.NONE,
        allow_divergent_steps: bool = False,
    ) -> None:
        self._profile = profile or AlignmentProfile.strict_audit_v1
        self._evaluator = evaluator or exact_equality_evaluator()
        self._minimum_priority = minimum_priority
        self._max_regression_level = max_regression_level
        self._allow_divergent_steps = allow_divergent_steps
        self._engine = TraceAlignmentEngine(
            AlignmentConfiguration(
                profile=self._profile, equivalence_evaluator=self._evaluator
            )
        )

    def check(self, golden: TraceRun, candidate: TraceRun) -> RegressionReport:
        """Align ``candidate`` against ``golden`` and return a verdict without raising."""
        result = self._engine.align(
            base=golden, comparison=candidate, minimum_priority=self._minimum_priority
        )

        steps_by_change: Dict[str, List[str]] = {}
        for alignment in result.alignments:
            kind = alignment.state.kind
            if kind == AlignmentStateKind.EXACT_MATCH:
                continue
            event = alignment.base_event or alignment.comparison_event
            label = event.payload.type_identifier if event is not None else "?"
            steps_by_change.setdefault(kind.value, []).append(label)

        risk = result.regression_risk
        level_ok = _LEVEL_ORDER[risk.level] <= _LEVEL_ORDER[self._max_regression_level]
        has_divergent = any(kind.value in steps_by_change for kind in _DIVERGENT_KINDS)
        steps_ok = self._allow_divergent_steps or not has_divergent
        passed = level_ok and steps_ok

        golden_fp = run_fingerprint(golden)
        candidate_fp = run_fingerprint(candidate)
        return RegressionReport(
            passed=passed,
            regression_level=risk.level,
            strength=risk.strength,
            reasoning=risk.reasoning,
            fingerprint_match=golden_fp == candidate_fp,
            golden_fingerprint=golden_fp,
            candidate_fingerprint=candidate_fp,
            steps_by_change=steps_by_change,
            max_regression_level=self._max_regression_level,
            allow_divergent_steps=self._allow_divergent_steps,
        )

    def assert_no_regression(
        self, golden: TraceRun, candidate: TraceRun
    ) -> RegressionReport:
        """Check and raise :class:`RegressionError` if the candidate regressed.

        Returns the (passing) report so callers can make further assertions on it.
        """
        report = self.check(golden, candidate)
        if not report.passed:
            raise RegressionError(report)
        return report


def assert_no_regression(
    golden: TraceRun,
    candidate: TraceRun,
    *,
    profile: Optional[AlignmentProfile] = None,
    evaluator: Optional[AnyEquivalenceEvaluator] = None,
    minimum_priority: TracePriority = TracePriority.STRUCTURAL,
    max_regression_level: RegressionLevel = RegressionLevel.NONE,
    allow_divergent_steps: bool = False,
) -> RegressionReport:
    """One-shot convenience: build a :class:`RegressionGate` and assert in one call."""
    return RegressionGate(
        profile=profile,
        evaluator=evaluator,
        minimum_priority=minimum_priority,
        max_regression_level=max_regression_level,
        allow_divergent_steps=allow_divergent_steps,
    ).assert_no_regression(golden, candidate)


__all__ = [
    "RegressionGate",
    "RegressionReport",
    "RegressionError",
    "assert_no_regression",
    "exact_equality_evaluator",
    "run_fingerprint",
]

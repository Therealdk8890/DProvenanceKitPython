"""Parity port of Swift RegressionRiskSoundnessTests.

Pins that RegressionRisk derives from the equivalence outcome — a critical step that is
removed, reordered relative to another critical step, or changed beyond equivalence fires
HIGH — instead of only reading the coarse removed/reordered display states (which
classified a materially-changed or skipped critical as ``ambiguous`` and reported
``NONE``). Also pins the opposite-direction false alarm: a benign structural step moving
past a stationary critical must NOT fire HIGH.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from dprovenancekit import (
    AlignmentConfiguration,
    AlignmentProfile,
    AnyEquivalenceEvaluator,
    RegressionLevel,
    TraceAlignmentEngine,
    TraceEvent,
    TracePriority,
    TraceableEvent,
    TraceRun,
)


@dataclass(frozen=True)
class Step(TraceableEvent):
    kind: str
    body: str = ""
    critical: bool = True

    @property
    def type_identifier(self) -> str:
        return self.kind

    @property
    def priority(self) -> TracePriority:
        return TracePriority.CRITICAL if self.critical else TracePriority.STRUCTURAL


def _run(specs):
    run_id = uuid.uuid4()
    events = [
        TraceEvent(
            run_id=run_id,
            context_id="ctx",
            engine_name="e",
            schema_version=1,
            sequence=seq,
            span_id=None,
            parent_span_id=None,
            payload=payload,
        )
        for seq, payload in specs
    ]
    return TraceRun(run_id=run_id, context_id="ctx", events=events)


def _engine():
    # Payload-equality evaluator: 1.0 iff payloads are identical, else 0.0.
    evaluator = AnyEquivalenceEvaluator(
        evaluator_identifier="eq", evaluator=lambda a, b: 1.0 if a == b else 0.0
    )
    return TraceAlignmentEngine(
        AlignmentConfiguration(AlignmentProfile.strict_audit_v1, evaluator)
    )


def test_materially_changed_critical_step_fires_high():
    base = _run([(0, Step("authorize_payment", "alice:100"))])
    comp = _run([(0, Step("authorize_payment", "attacker:1000000"))])
    result = _engine().align(base, comp)
    assert result.regression_risk.level is RegressionLevel.HIGH
    assert result.regression_risk.strength == 0.9


def test_skipped_critical_masked_by_same_type_decoy_fires_high():
    # validate_permissions is skipped; send_receipt is new; both share type "decision".
    base = _run([(0, Step("decision", "validate_permissions")), (1, Step("decision", "charge_card"))])
    comp = _run([(0, Step("decision", "send_receipt")), (1, Step("decision", "charge_card"))])
    result = _engine().align(base, comp)
    # The critical validate_permissions binds to the decoy but scores below threshold →
    # changed beyond equivalence → HIGH (not a silent NONE).
    assert result.regression_risk.level is RegressionLevel.HIGH


def test_reordered_critical_steps_fire_high():
    base = _run([(0, Step("createCustomer", "x")), (1, Step("generateInvoice", "y"))])
    comp = _run([(0, Step("generateInvoice", "y")), (1, Step("createCustomer", "x"))])
    result = _engine().align(base, comp)
    assert result.regression_risk.level is RegressionLevel.HIGH
    assert result.regression_risk.strength == 1.0


def test_benign_structural_reorder_does_not_fire_false_high():
    # Only the STRUCTURAL log moves; the critical authorize does not move relative to any
    # other critical step, so there is no regression.
    base = _run([(0, Step("log", "l", critical=False)), (1, Step("authorize", "a"))])
    comp = _run([(0, Step("authorize", "a")), (1, Step("log", "l", critical=False))])
    result = _engine().align(base, comp)
    assert result.regression_risk.level is RegressionLevel.NONE


def test_equivalent_step_is_not_a_regression():
    base = _run([(0, Step("authorize", "same")), (1, Step("finalize", "ok"))])
    comp = _run([(0, Step("authorize", "same")), (1, Step("finalize", "ok"))])
    result = _engine().align(base, comp)
    assert result.regression_risk.level is RegressionLevel.NONE

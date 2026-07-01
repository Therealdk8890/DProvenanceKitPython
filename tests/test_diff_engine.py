"""Ports TraceDiffEngineTests."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from dprovenancekit import (
    ChangeKind,
    TraceDiffEngine,
    TraceEvent,
    TracePriority,
    TraceableEvent,
    TraceRun,
)


@dataclass(frozen=True)
class DiffEvent(TraceableEvent):
    name: str
    noise_value: int = -1

    @property
    def type_identifier(self) -> str:
        if self.noise_value >= 0:
            return f"noise_{self.noise_value}"
        return self.name

    @property
    def priority(self) -> TracePriority:
        return (
            TracePriority.TELEMETRY
            if self.noise_value >= 0
            else TracePriority.STRUCTURAL
        )

    @staticmethod
    def step(name):
        return DiffEvent(name)

    @staticmethod
    def noise(val):
        return DiffEvent("noise", noise_value=val)


def _run(run_id, seq_events):
    events = [
        TraceEvent(
            run_id=run_id,
            context_id="test_ctx",
            engine_name=engine,
            schema_version=1,
            sequence=seq,
            span_id=None,
            parent_span_id=None,
            payload=payload,
        )
        for (seq, engine, payload) in seq_events
    ]
    return TraceRun(run_id=run_id, context_id="test_ctx", events=events)


def test_causal_diff_ignores_telemetry():
    run_a, run_b = uuid.uuid4(), uuid.uuid4()
    base = _run(
        run_a,
        [
            (0, "engine1", DiffEvent.step("stepA")),
            (1, "engine1", DiffEvent.noise(1)),
            (2, "engine1", DiffEvent.step("stepB")),
            (3, "engine1", DiffEvent.noise(2)),
            (4, "engine1", DiffEvent.step("stepC")),
        ],
    )
    comp = _run(
        run_b,
        [
            (0, "engine1", DiffEvent.step("stepA")),
            (1, "engine1", DiffEvent.noise(3)),
            (2, "engine1", DiffEvent.step("stepB")),
            (3, "engine1", DiffEvent.step("stepC")),
            (4, "engine1", DiffEvent.noise(4)),
        ],
    )
    diff = TraceDiffEngine().diff(base, comp, minimum_priority=TracePriority.STRUCTURAL)
    assert diff.is_identical
    assert len(diff.changes) == 0


def test_causal_diff_detects_removal_and_addition():
    run_a, run_b = uuid.uuid4(), uuid.uuid4()
    base = _run(
        run_a,
        [
            (0, "engine1", DiffEvent.step("stepA")),
            (1, "engine1", DiffEvent.step("stepB")),
            (2, "engine1", DiffEvent.step("stepC")),
        ],
    )
    comp = _run(
        run_b,
        [
            (0, "engine1", DiffEvent.step("stepA")),
            (1, "engine1", DiffEvent.step("stepC")),
            (2, "engine1", DiffEvent.step("stepD")),
        ],
    )
    diff = TraceDiffEngine().diff(base, comp, minimum_priority=TracePriority.STRUCTURAL)
    assert not diff.is_identical
    assert len(diff.changes) == 2

    removal = next(c for c in diff.changes if c.kind == ChangeKind.REMOVED)
    assert removal.type_identifier == "stepB"
    assert removal.original_sequence == 1

    addition = next(c for c in diff.changes if c.kind == ChangeKind.ADDED)
    assert addition.type_identifier == "stepD"
    assert addition.original_sequence == 2


def test_engine_name_causes_divergence():
    run_a, run_b = uuid.uuid4(), uuid.uuid4()
    base = _run(
        run_a,
        [
            (0, "engine1", DiffEvent.step("stepA")),
            (1, "engine1", DiffEvent.step("stepB")),
        ],
    )
    comp = _run(
        run_b,
        [
            (0, "engine1", DiffEvent.step("stepA")),
            (1, "engine2", DiffEvent.step("stepB")),
        ],
    )
    diff = TraceDiffEngine().diff(base, comp, minimum_priority=TracePriority.STRUCTURAL)
    assert not diff.is_identical
    assert len(diff.changes) == 2

    removal = next(c for c in diff.changes if c.kind == ChangeKind.REMOVED)
    assert removal.engine_name == "engine1"
    assert removal.type_identifier == "stepB"

    addition = next(c for c in diff.changes if c.kind == ChangeKind.ADDED)
    assert addition.engine_name == "engine2"
    assert addition.type_identifier == "stepB"

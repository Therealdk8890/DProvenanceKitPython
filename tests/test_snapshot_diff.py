"""Ports SnapshotDiffEngineTests."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from dprovenancekit import (
    EventChangeKind,
    SnapshotDiffEngine,
    SpanChangeKind,
    TraceEvent,
    TracePriority,
    TraceReplayEngine,
    TraceableEvent,
)


@dataclass(frozen=True)
class MockEvent(TraceableEvent):
    kind: str  # start | middle | end
    label: str = ""

    @property
    def type_identifier(self) -> str:
        return self.kind

    @property
    def priority(self) -> TracePriority:
        return TracePriority.STRUCTURAL

    @staticmethod
    def start():
        return MockEvent("start")

    @staticmethod
    def middle(label):
        return MockEvent("middle", label)

    @staticmethod
    def end():
        return MockEvent("end")


def _event(run_id, seq, span_id, parent_span_id, payload, event_id=None):
    return TraceEvent(
        id=event_id if event_id is not None else uuid.uuid4(),
        run_id=run_id,
        context_id="test_context",
        engine_name="test_engine",
        schema_version=1,
        sequence=seq,
        span_id=span_id,
        parent_span_id=parent_span_id,
        payload=payload,
    )


def test_temporal_additions():
    run_id = uuid.uuid4()
    span_a = str(uuid.uuid4())
    e1 = _event(run_id, 1, span_a, None, MockEvent.start())
    e2 = _event(run_id, 2, span_a, None, MockEvent.middle("A"))

    base = TraceReplayEngine([e1]).snapshot()
    comp = TraceReplayEngine([e1, e2]).snapshot()
    diff = SnapshotDiffEngine().diff(base, comp)

    assert diff.summary.added_spans == 0
    assert diff.summary.added_events == 1
    assert diff.summary.modified_events == 0
    assert diff.summary.divergence_points == 0

    first = diff.event_changes[0]
    assert first.kind == EventChangeKind.ADDED
    assert first.event.event.sequence == 2
    assert first.span_id == span_a


def test_payload_modifications():
    run_id = uuid.uuid4()
    event_id = uuid.uuid4()
    span_a = str(uuid.uuid4())
    e1 = _event(run_id, 1, span_a, None, MockEvent.middle("A"), event_id)
    e1_mod = _event(run_id, 1, span_a, None, MockEvent.middle("B"), event_id)

    base = TraceReplayEngine([e1]).snapshot()
    comp = TraceReplayEngine([e1_mod]).snapshot()
    diff = SnapshotDiffEngine().diff(base, comp)

    assert diff.summary.added_events == 0
    assert diff.summary.removed_events == 0
    assert diff.summary.modified_events == 1
    assert diff.summary.divergence_points == 1

    first = diff.event_changes[0]
    assert first.kind == EventChangeKind.MODIFIED
    assert first.before.event.payload == MockEvent.middle("A")
    assert first.after.event.payload == MockEvent.middle("B")


def test_span_reparenting():
    run_id = uuid.uuid4()
    span_a, span_b, span_c = "spanA", "spanB", "spanC"
    root_a = _event(run_id, 0, span_a, None, MockEvent.start())
    root_b = _event(run_id, 0, span_b, None, MockEvent.start())
    base_event = _event(run_id, 1, span_c, span_a, MockEvent.start())
    comp_event = _event(run_id, 1, span_c, span_b, MockEvent.start())

    base = TraceReplayEngine([root_a, root_b, base_event]).snapshot()
    comp = TraceReplayEngine([root_a, root_b, comp_event]).snapshot()
    diff = SnapshotDiffEngine().diff(base, comp)

    assert len(diff.span_changes) == 1
    sc = diff.span_changes[0]
    assert sc.kind == SpanChangeKind.REPARENTED
    assert sc.span_id == span_c
    assert sc.from_parent == span_a
    assert sc.to_parent == span_b


def test_contamination_changes():
    run_id = uuid.uuid4()
    span_a = "spanA"
    e1 = _event(run_id, 1, span_a, None, MockEvent.start())
    e2 = _event(run_id, 2, span_a, None, MockEvent.end())

    base = TraceReplayEngine([e1, e2]).snapshot()
    comp = TraceReplayEngine([e1], quarantined=[e2]).snapshot()
    diff = SnapshotDiffEngine().diff(base, comp)

    assert diff.summary.contaminated_spans == 1
    assert diff.summary.modified_events == 1


def test_structural_divergence():
    run_id = uuid.uuid4()
    span_a = "spanA"
    e1 = _event(run_id, 1, span_a, None, MockEvent.start())
    e2_base = _event(run_id, 2, span_a, None, MockEvent.middle("Branch 1"))
    e2_comp = _event(run_id, 2, span_a, None, MockEvent.middle("Branch 2"))

    base = TraceReplayEngine([e1, e2_base]).snapshot()
    comp = TraceReplayEngine([e1, e2_comp]).snapshot()
    diff = SnapshotDiffEngine().diff(base, comp)

    assert diff.summary.divergence_points == 1
    d = diff.divergences[0]
    assert d.span_id == span_a
    assert d.common_prefix_length == 1
    assert d.divergence_sequence == 2
    assert d.left_event.event.payload == MockEvent.middle("Branch 1")
    assert d.right_event.event.payload == MockEvent.middle("Branch 2")


def test_diff_symmetry():
    run_id = uuid.uuid4()
    span_a = "spanA"
    e2_added = _event(run_id, 2, span_a, None, MockEvent.middle("added"))
    mod_id = uuid.uuid4()
    e1_before = _event(run_id, 1, span_a, None, MockEvent.middle("before"), mod_id)
    e1_after = _event(run_id, 1, span_a, None, MockEvent.middle("after"), mod_id)

    base = TraceReplayEngine([e1_before]).snapshot()
    comp = TraceReplayEngine([e1_after, e2_added]).snapshot()

    engine = SnapshotDiffEngine()
    diff_ab = engine.diff(base, comp)
    diff_ba = engine.diff(comp, base)

    assert diff_ab.summary.added_events == 1
    assert diff_ab.summary.modified_events == 1
    assert diff_ab.summary.removed_events == 0

    assert diff_ba.summary.added_events == 0
    assert diff_ba.summary.modified_events == 1
    assert diff_ba.summary.removed_events == 1

    assert diff_ab.summary.divergence_points == diff_ba.summary.divergence_points
    if diff_ab.divergences and diff_ba.divergences:
        assert (
            diff_ab.divergences[0].left_event.event.id
            == diff_ba.divergences[0].right_event.event.id
        )
        assert (
            diff_ab.divergences[0].right_event.event.id
            == diff_ba.divergences[0].left_event.event.id
        )


@dataclass(frozen=True)
class _MaskedPayload(TraceableEvent):
    label: str
    hidden_state: int

    @property
    def type_identifier(self) -> str:
        return "masked"

    @property
    def priority(self) -> TracePriority:
        return TracePriority.STRUCTURAL

    def to_dict(self) -> dict:
        return {"label": self.label}  # hidden_state is intentionally excluded


def test_diff_compares_payload_value_not_encoded_hash():
    run_id = uuid.uuid4()
    event_id = uuid.uuid4()
    span_a = "spanA"

    def event(payload):
        return _event(run_id, 1, span_a, None, payload, event_id)

    a = _MaskedPayload(label="x", hidden_state=1)
    b = _MaskedPayload(label="x", hidden_state=2)

    assert a.encode() == b.encode()  # encodings collide...
    assert a != b  # ...but the values differ

    base = TraceReplayEngine([event(a)]).snapshot()
    comp = TraceReplayEngine([event(b)]).snapshot()
    diff = SnapshotDiffEngine().diff(base, comp)

    assert diff.summary.modified_events == 1
    assert diff.summary.divergence_points == 1

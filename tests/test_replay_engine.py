"""Ports TraceReplayEngineTests."""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass

from dprovenancekit import TraceEvent, TracePriority, TraceReplayEngine, TraceableEvent


@dataclass(frozen=True)
class MockEvent(TraceableEvent):
    kind: str

    @property
    def type_identifier(self) -> str:
        return self.kind

    @property
    def priority(self) -> TracePriority:
        return TracePriority.STRUCTURAL


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


def test_duplicate_event_ids():
    run_id = uuid.uuid4()
    event_id = uuid.uuid4()
    span_id = str(uuid.uuid4())
    committed = _event(run_id, 1, span_id, None, MockEvent("start"), event_id)
    quarantined = _event(run_id, 1, span_id, None, MockEvent("start"), event_id)

    snapshot = TraceReplayEngine([committed], quarantined=[quarantined]).snapshot()
    assert len(snapshot.roots) == 1
    assert len(snapshot.roots[0].events) == 2
    assert snapshot.manifest.total_events == 2
    assert snapshot.manifest.committed_events == 1
    assert snapshot.manifest.quarantined_events == 1
    assert snapshot.roots[0].contains_quarantined_events


def test_malformed_parent_relationships():
    run_id = uuid.uuid4()
    span_a = str(uuid.uuid4())
    span_b = str(uuid.uuid4())
    child = _event(run_id, 2, span_b, span_a, MockEvent("middle"))
    grandchild = _event(run_id, 3, str(uuid.uuid4()), span_b, MockEvent("end"))

    snapshot = TraceReplayEngine([child, grandchild]).snapshot()
    assert len(snapshot.roots) == 0
    assert len(snapshot.orphaned_events) == 2
    assert snapshot.manifest.orphaned_events == 2


def test_replay_determinism():
    run_id = uuid.uuid4()
    span_a = str(uuid.uuid4())
    span_b = str(uuid.uuid4())

    events = []
    for i in range(50):
        parent = span_a if i % 2 == 0 else span_b
        events.append(_event(run_id, i, str(uuid.uuid4()), parent, MockEvent("middle")))
    events.append(_event(run_id, 100, span_a, None, MockEvent("start")))
    events.append(_event(run_id, 101, span_b, None, MockEvent("start")))

    baseline = TraceReplayEngine(events).snapshot()

    rng = random.Random(1234)
    for _ in range(100):
        shuffled = list(events)
        rng.shuffle(shuffled)
        split = rng.randrange(0, len(shuffled))
        committed = shuffled[:split]
        quarantined = shuffled[split:]
        snapshot = TraceReplayEngine(committed, quarantined=quarantined).snapshot()

        assert len(snapshot.roots) == len(baseline.roots)
        assert len(snapshot.orphaned_events) == len(baseline.orphaned_events)
        assert snapshot.manifest.total_events == baseline.manifest.total_events
        assert snapshot.manifest.sequence_gaps == baseline.manifest.sequence_gaps
        assert (
            snapshot.manifest.reconstructed_spans
            == baseline.manifest.reconstructed_spans
        )


def test_sequence_gaps():
    run_id = uuid.uuid4()
    events = [
        _event(run_id, 1, None, None, MockEvent("start")),
        _event(run_id, 2, None, None, MockEvent("start")),
        _event(run_id, 5, None, None, MockEvent("start")),
        _event(run_id, 6, None, None, MockEvent("start")),
        _event(run_id, 10, None, None, MockEvent("start")),
    ]
    snapshot = TraceReplayEngine(events).snapshot()
    gaps = snapshot.manifest.sequence_gaps
    assert len(gaps) == 3
    assert (gaps[0].lower_bound, gaps[0].upper_bound) == (0, 0)
    assert (gaps[1].lower_bound, gaps[1].upper_bound) == (3, 4)
    assert (gaps[2].lower_bound, gaps[2].upper_bound) == (7, 9)


def test_span_parent_cycle_events_are_orphaned_not_dropped():
    """A parent cycle (A<->B) or a self-parent leaves its spans neither rooted nor
    reachable. The old orphan test only fired on a *missing* parent, so cycle members
    silently vanished from the tree while the manifest still counted them. They must now
    surface as orphaned events so nothing is lost without accounting."""
    run_id = uuid.uuid4()
    events = [
        _event(run_id, 0, "A", "B", MockEvent("a")),  # A's parent is B
        _event(run_id, 1, "B", "A", MockEvent("b")),  # B's parent is A -> cycle
        _event(run_id, 2, "C", None, MockEvent("c")),  # healthy root span
    ]
    snap = TraceReplayEngine(events).snapshot()

    def tree_event_count(roots):
        total, stack, seen = 0, list(roots), set()
        while stack:
            node = stack.pop()
            if id(node) in seen:
                continue
            seen.add(id(node))
            total += len(node.events)
            stack.extend(node.children)
        return total

    in_tree = tree_event_count(snap.roots)
    assert snap.manifest.total_events == 3
    assert snap.manifest.orphaned_events == 2  # A and B
    # Every event is accounted for: in the tree or explicitly orphaned, none dropped.
    assert in_tree + snap.manifest.orphaned_events == snap.manifest.total_events


def test_self_parent_span_is_orphaned_not_dropped():
    run_id = uuid.uuid4()
    events = [
        _event(run_id, 0, "S", "S", MockEvent("s")),  # self-parent
        _event(run_id, 1, "C", None, MockEvent("c")),
    ]
    snap = TraceReplayEngine(events).snapshot()
    assert snap.manifest.total_events == 2
    assert snap.manifest.orphaned_events == 1

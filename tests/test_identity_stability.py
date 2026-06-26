"""Ports IdentityStabilityTests (the pure view-model layer)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from dprovenancekit import (
    RenderHints,
    SpanViewModel,
    TraceEvent,
    TracePriority,
    TraceReplayEngine,
    TraceableEvent,
    flatten_span_tree,
)


@dataclass(frozen=True)
class MockUIEvent(TraceableEvent):
    @property
    def type_identifier(self) -> str:
        return "TestEvent"

    @property
    def priority(self) -> TracePriority:
        return TracePriority.DIAGNOSTIC


def _event(run_id, seq, span_id, parent_span_id):
    return TraceEvent(
        run_id=run_id, context_id="ctx", engine_name="engine", schema_version=1,
        sequence=seq, span_id=span_id, parent_span_id=parent_span_id, payload=MockUIEvent(),
    )


def test_stable_identity_across_snapshots():
    run_id = uuid.uuid4()
    span_a = "spanA"
    e1 = _event(run_id, 1, span_a, None)
    snap1 = TraceReplayEngine([e1]).snapshot()
    e2 = _event(run_id, 2, span_a, None)
    snap2 = TraceReplayEngine([e1, e2]).snapshot()

    hints = RenderHints()
    vms1 = [SpanViewModel.from_node(r, snapshot_id="snap_1", local_path_hash="hash1", depth=0, hints=hints) for r in snap1.roots]
    vms2 = [SpanViewModel.from_node(r, snapshot_id="snap_2", local_path_hash="hash1", depth=0, hints=hints) for r in snap2.roots]

    assert len(vms1) == 1
    assert len(vms2) == 1
    assert vms1[0].render_id == "spanA::snap_1::hash1"
    assert vms2[0].render_id == "spanA::snap_2::hash1"


def test_no_duplicate_render_ids_in_flattened_output():
    run_id = uuid.uuid4()
    events = [
        _event(run_id, 1, "root", None),
        _event(run_id, 2, "child1", "root"),
        _event(run_id, 3, "child2", "root"),
    ]
    snap = TraceReplayEngine(events).snapshot()
    hints = RenderHints(collapsed_by_default=set())
    root_models = [SpanViewModel.from_node(r, snapshot_id="snap_1", local_path_hash="baseHash", depth=0, hints=hints) for r in snap.roots]

    flattened = flatten_span_tree(root_models, dynamic_collapsed=set())
    ids = [f.id for f in flattened]
    assert len(ids) == len(set(ids))
    assert len(ids) == 3
    assert all(f.is_visible for f in flattened)

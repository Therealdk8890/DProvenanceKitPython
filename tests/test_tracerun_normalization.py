"""Ports TraceRunNormalizationTests.

Pins the ``TraceRun`` construction invariant: ``events`` is always in ascending
``sequence`` order (ties keep the caller's relative order), so causal analysis can
never depend on the order a caller assembled the list. Before this, the in-memory
temporal query evaluator (``after``/``before``) used list order — diverging from
both the SQL backend (``MIN(sequence)``) and TRACE_SPEC_v1 — and the diff engine
manufactured spurious added/removed pairs for hand-assembled out-of-order runs.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from dprovenancekit import (
    TraceDiffEngine,
    TraceEvent,
    TracePriority,
    TraceQueryDSL,
    TraceableEvent,
    TraceRun,
)


@dataclass(frozen=True)
class Step(TraceableEvent):
    name: str
    body: str = "b"

    @property
    def type_identifier(self) -> str:
        return self.name

    @property
    def priority(self) -> TracePriority:
        return TracePriority.CRITICAL


def _ev(seq: int, name: str, body: str = "b") -> TraceEvent:
    return TraceEvent(
        run_id=uuid.UUID("40000000-0000-0000-0000-000000000001"),
        context_id="ctx",
        engine_name="e",
        schema_version=1,
        sequence=seq,
        span_id=None,
        parent_span_id=None,
        payload=Step(name=name, body=body),
    )


def _run(events) -> TraceRun:
    return TraceRun(run_id=uuid.uuid4(), context_id="ctx", events=events)


def test_out_of_order_list_is_sorted_by_sequence():
    run = _run([_ev(2, "C"), _ev(0, "A"), _ev(1, "B")])
    assert [e.sequence for e in run.events] == [0, 1, 2]
    assert [e.payload.name for e in run.events] == ["A", "B", "C"]


def test_already_sorted_list_is_preserved_verbatim():
    events = [_ev(0, "A"), _ev(1, "B"), _ev(2, "C")]
    run = _run(events)
    assert [e.id for e in run.events] == [e.id for e in events]


def test_duplicate_sequences_keep_assembly_order():
    first, second = _ev(1, "dup", "first"), _ev(1, "dup", "second")
    run = _run([_ev(2, "tail"), first, second, _ev(0, "head")])
    assert [e.sequence for e in run.events] == [0, 1, 1, 2]
    assert run.events[1].payload.body == "first"
    assert run.events[2].payload.body == "second"


def test_temporal_query_matches_sql_semantics_on_unsorted_input():
    # Causally A(0) runs before B(1); the list was assembled B-first. The
    # in-memory evaluator used list order here and answered False, while the
    # SQL backend (MIN(sequence)) answered True for the same run.
    run = _run([_ev(1, "B"), _ev(0, "A")])
    query = TraceQueryDSL().requiring_followed_by("A", followed_by="B")
    assert query.ast.evaluate(run) is True


def test_diff_is_assembly_order_invariant():
    sorted_run = _run([_ev(0, "authorize", "a"), _ev(1, "charge", "c")])
    shuffled = _run([_ev(1, "charge", "c"), _ev(0, "authorize", "a")])
    result = TraceDiffEngine().diff(base=sorted_run, comparison=shuffled)
    assert result.is_identical, (
        "same events, same causal order — assembly order must not manufacture changes"
    )

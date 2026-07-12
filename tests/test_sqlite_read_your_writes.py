"""Read-your-writes on the SQLite store's query path.

Events sit in the priority write buffer until the background writer's next tick
(idle cadence ~500ms), so a query issued moments after recording must flush first
or freshly recorded runs are invisible — which surfaces as flaky "missing step"
failures in CI gates that record and immediately gate. ``query_runs`` is the
regression target; ``get_run`` and ``list_run_metadata`` already flushed.
"""

from __future__ import annotations

from dprovenancekit import (
    DProvenanceKit,
    SQLiteTraceStore,
    TracePriority,
    TraceQueryDSL,
)
from dprovenancekit.event import AnyTraceableEvent


def _record_run(store, context_id="rw-ctx"):
    kit = DProvenanceKit(AnyTraceableEvent)
    with kit.run(context_id=context_id, store=store) as run:
        kit.record(
            AnyTraceableEvent(
                type_identifier_value="step.one",
                priority_value=int(TracePriority.STRUCTURAL),
                raw_json="{}",
            )
        )
    return run.run_id


def test_query_runs_sees_just_recorded_events_without_explicit_flush(tmp_path):
    path = str(tmp_path / "rw.sqlite")
    with SQLiteTraceStore(AnyTraceableEvent, path) as store:
        run_id = _record_run(store)
        # No store.flush() here: query_runs must flush internally.
        runs = store.query_runs(TraceQueryDSL().filter_context_id("rw-ctx"))
        assert [r.run_id for r in runs] == [run_id]
        assert [e.payload.type_identifier for e in runs[0].events] == ["step.one"]

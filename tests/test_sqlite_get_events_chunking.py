"""SQLiteTraceStore.get_events must handle graphs larger than SQLite's bound-parameter
limit (999 before 3.32). A single IN clause with one placeholder per id raises
"too many SQL variables" on the older library versions some LTS distros still ship;
get_events chunks the query so a large id set round-trips intact.
"""

from __future__ import annotations

from dprovenancekit import DProvenanceKit, SQLiteTraceStore, TracePriority
from dprovenancekit.event import AnyTraceableEvent


def _record_many(store, n):
    kit = DProvenanceKit(AnyTraceableEvent)
    ids = []
    with kit.run(context_id="big", store=store):
        for i in range(n):
            eid = kit.record(
                AnyTraceableEvent(
                    type_identifier_value=f"step.{i}",
                    priority_value=int(TracePriority.STRUCTURAL),
                    raw_json="{}",
                )
            )
            ids.append(eid)
    return ids


def test_get_events_handles_more_ids_than_sqlite_variable_limit(tmp_path):
    path = str(tmp_path / "big.sqlite")
    n = 2100  # comfortably over the 999 pre-3.32 limit and the 900 chunk size
    with SQLiteTraceStore(AnyTraceableEvent, path) as store:
        ids = _record_many(store, n)
        fetched = store.get_events(set(ids))
        assert len(fetched) == n
        assert set(fetched.keys()) == set(ids)
        # Chunk boundaries must not corrupt payloads.
        for eid, event in fetched.items():
            assert event.payload.type_identifier.startswith("step.")

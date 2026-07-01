"""Ports RawTraceStoreRoundTripTests: the viewer's read path."""

from __future__ import annotations

import json

from dprovenancekit import DProvenanceKit, RawTraceStore, SQLiteTraceStore
from conftest import TestEvent


def test_written_run_survives_raw_trace_store_reopen(temp_db_path):
    store = SQLiteTraceStore(TestEvent, temp_db_path)
    kit = DProvenanceKit(TestEvent)
    with kit.run(context_id="roundtrip", store=store):
        kit.record(TestEvent.process_started())
        kit.record(TestEvent.step_completed(7))
        kit.record(TestEvent.error_detected())
        kit.record(TestEvent.process_finished())
    store.flush()

    reader = RawTraceStore(temp_db_path)
    runs = reader.fetch_all_runs()

    assert len(runs) == 1
    run = runs[0]
    assert run.context_id == "roundtrip"
    assert run.event_count == 4
    assert len(run.events) == 4

    assert [e.type_identifier for e in run.events] == [
        "processStarted",
        "stepCompleted",
        "errorDetected",
        "processFinished",
    ]
    assert [e.sequence for e in run.events] == [0, 1, 2, 3]

    for raw in run.events:
        assert raw.payload_json
        assert raw.payload_json != "{}"
        decoded = TestEvent.decode(raw.payload_json.encode("utf-8"))
        assert decoded.type_identifier == raw.type_identifier

    step = next(e for e in run.events if e.type_identifier == "stepCompleted")
    decoded_step = TestEvent.from_dict(json.loads(step.payload_json))
    assert decoded_step == TestEvent.step_completed(7)

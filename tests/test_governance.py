from dprovenancekit import InMemoryTraceStore, record_governance_event, traced_run


def test_record_governance_event_maps_controller_envelope_and_attributes():
    store = InMemoryTraceStore()
    event = {
        "event_id": "evt-1",
        "event_type": "verification_evaluated",
        "timestamp": 123.0,
        "agent_id": "agent-1",
        "trace_id": "trace-1",
        "run_id": "run-1",
        "action_id": "action-1",
        "attributes": {
            "report_fingerprint": "report-1",
            "disposition": "block",
        },
    }

    with traced_run(store, context_id="agent-1") as run:
        event_id = record_governance_event(event)

    assert event_id is not None
    recorded = store.get_run(run.run_id)
    assert recorded is not None
    assert len(recorded.events) == 1
    payload = recorded.events[0].payload
    assert payload.type_identifier == "agent_containment.verification_evaluated"
    assert payload.attributes["event_id"] == "evt-1"
    assert payload.attributes["trace_id"] == "trace-1"
    assert payload.attributes["attribute.report_fingerprint"] == "report-1"
    assert payload.attributes["attribute.disposition"] == "block"


def test_record_governance_event_rejects_missing_controller_identity():
    store = InMemoryTraceStore()

    with traced_run(store, context_id="agent-1"):
        try:
            record_governance_event({"event_type": "verification_evaluated"})
        except ValueError as exc:
            assert "event_id" in str(exc)
        else:
            raise AssertionError("expected ValueError")

"""Ports SpanTreeTests: nested spans recorded through ambient context."""

from __future__ import annotations

from dataclasses import dataclass

from dprovenancekit import DProvenanceKit, SQLiteTraceStore, TracePriority, TraceQueryDSL, TraceableEvent


@dataclass(frozen=True)
class SpanEvent(TraceableEvent):
    kind: str

    @property
    def type_identifier(self) -> str:
        return self.kind

    @property
    def priority(self) -> TracePriority:
        if self.kind in ("taskStarted", "taskCompleted"):
            return TracePriority.STRUCTURAL
        return TracePriority.TELEMETRY

    def to_dict(self):
        return {"kind": self.kind}

    @classmethod
    def from_dict(cls, data):
        return cls(data["kind"])


def test_nested_spans(temp_db_path):
    store = SQLiteTraceStore(SpanEvent, temp_db_path)
    kit = DProvenanceKit(SpanEvent)

    with kit.run(context_id="hierarchy_test_1", store=store):
        kit.record(SpanEvent("taskStarted"))  # root: no span
        with kit.with_span():
            kit.record(SpanEvent("processingData"))
            with kit.with_span():
                kit.record(SpanEvent("taskCompleted"))

    store.flush()

    runs = store.query_runs(TraceQueryDSL())
    assert len(runs) == 1
    events = runs[0].events
    assert len(events) == 3

    root = next(e for e in events if e.payload.type_identifier == "taskStarted")
    assert root.span_id is None
    assert root.parent_span_id is None

    child = next(e for e in events if e.payload.type_identifier == "processingData")
    assert child.span_id is not None
    assert child.parent_span_id is None  # parent was the root

    grandchild = next(e for e in events if e.payload.type_identifier == "taskCompleted")
    assert grandchild.span_id is not None
    assert grandchild.parent_span_id == child.span_id
    assert grandchild.span_id != child.span_id

# git-blob-rewrite

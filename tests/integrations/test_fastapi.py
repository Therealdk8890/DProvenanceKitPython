import uuid

import pytest
from dprovenancekit.kit import DProvenanceKit
from dprovenancekit.store import InMemoryTraceStore
from dprovenancekit.event import TraceableEvent
from dataclasses import dataclass

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi import FastAPI
from fastapi.testclient import TestClient
from dprovenancekit.integrations.fastapi import DProvenanceMiddleware


class APIEvent(TraceableEvent):
    def __init__(self, t, p, r):
        self._t = t
        self._p = p
        self._r = r
    @property
    def type_identifier(self):
        return self._t
    @property
    def priority(self):
        return self._p
    def to_dict(self):
        return {"j": self._r}
    
    
    

    @classmethod
    def handled_request(cls):
        return cls("handled_request", 1, '{"status": "ok"}')


def test_fastapi_middleware():
    kit = DProvenanceKit(APIEvent)
    store = InMemoryTraceStore()
    
    app = FastAPI()
    app.add_middleware(DProvenanceMiddleware, kit=kit, store=store)
    
    @app.get("/test")
    def read_test():
        kit.record(APIEvent.handled_request())
        return {"hello": "world"}
        
    client = TestClient(app)
    response = client.get("/test")
    
    assert response.status_code == 200
    assert response.json() == {"hello": "world"}
    
    trace_id = response.headers.get("X-DProvenance-Trace-Id")
    assert trace_id is not None
    
    run = store.get_run(uuid.UUID(trace_id))
    assert run is not None
    assert run.context_id == "GET /test"
    assert len(run.events) == 1
    
    assert run.events[0].payload.type_identifier == "handled_request"

import pytest
from dprovenancekit.kit import DProvenanceKit
from dprovenancekit.store import InMemoryTraceStore
from dprovenancekit.event import TraceableEvent
from dataclasses import dataclass

pytest.importorskip("IPython")

from IPython.testing.globalipapp import get_ipython
from dprovenancekit.integrations.jupyter import load_ipython_extension


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
    def cell_step(cls):
        return cls("cell_step", 1, '{"status": "ok"}')


def test_jupyter_magic():
    ip = get_ipython()
    if ip is None:
        pytest.skip("IPython environment not available for testing")
        
    load_ipython_extension(ip)
    
    kit = DProvenanceKit(APIEvent)
    store = InMemoryTraceStore()
    
    # Inject variables into IPython namespace
    ip.user_ns["my_kit"] = kit
    ip.user_ns["my_store"] = store
    ip.user_ns["APIEvent"] = APIEvent
    
    # Run the magic
    ip.run_cell_magic(
        "trace_run", 
        "my_kit my_store", 
        "my_kit.record(APIEvent.cell_step())"
    )
    
    # Verify the trace was created and recorded the event
    assert len(store._events_by_run) == 1
    run_id = list(store._events_by_run.keys())[0]
    
    run = store.get_run(run_id)
    assert run.context_id == "jupyter-cell"
    assert len(run.events) == 1

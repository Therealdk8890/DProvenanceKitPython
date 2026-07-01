import pytest
from dprovenancekit.kit import DProvenanceKit
from dprovenancekit.store import InMemoryTraceStore
from dprovenancekit.event import TraceableEvent
from dataclasses import dataclass
import asyncio

from dprovenancekit.integrations.mcp import traced_mcp_tool


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
    def tool_start(cls, name, args):
        return cls("tool_start", 1, f'{{"name": "{name}", "args": "{args}"}}')
        
    @classmethod
    def tool_end(cls, name, res):
        return cls("tool_end", 1, f'{{"name": "{name}", "res": "{res}"}}')
        
    @classmethod
    def tool_error(cls, name, err):
        return cls("tool_error", 1, f'{{"name": "{name}", "err": "{err}"}}')

def test_mcp_traced_tool():
    kit = DProvenanceKit(APIEvent)
    store = InMemoryTraceStore()
    
    @traced_mcp_tool(
        kit=kit,
        start_event_factory=APIEvent.tool_start,
        end_event_factory=APIEvent.tool_end,
        error_event_factory=APIEvent.tool_error
    )
    def my_tool(x, y=10):
        if x < 0:
            raise ValueError("Negative")
        return x + y
        
    with kit.run("test", store=store) as run:
        assert my_tool(5) == 15
        
        with pytest.raises(ValueError):
            my_tool(-1)
            
    events = store.get_run(run.run_id).events #(run.run_id)
    assert len(events) == 4
    
    assert events[0].payload.type_identifier == "tool_start"
    assert '"args": "{' in events[0].payload.to_dict()["j"]
    assert events[1].payload.type_identifier == "tool_end"
    assert '"res": "15"' in events[1].payload.to_dict()["j"]
    
    assert events[2].payload.type_identifier == "tool_start"
    assert events[3].payload.type_identifier == "tool_error"

@pytest.mark.asyncio
async def test_mcp_traced_tool_async():
    kit = DProvenanceKit(APIEvent)
    store = InMemoryTraceStore()
    
    @traced_mcp_tool(
        kit=kit,
        start_event_factory=APIEvent.tool_start,
        end_event_factory=APIEvent.tool_end,
        error_event_factory=APIEvent.tool_error
    )
    async def my_async_tool(x):
        await asyncio.sleep(0.01)
        return x * 2
        
    with kit.run("test2", store=store) as run:
        assert await my_async_tool(4) == 8
        
    events = store.get_run(run.run_id).events #(run.run_id)
    assert len(events) == 2
    assert events[1].payload.type_identifier == "tool_end"
    assert '"res": "8"' in events[1].payload.to_dict()["j"]

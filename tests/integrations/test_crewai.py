import uuid

import pytest

from dprovenancekit.event import TraceableEvent
from dprovenancekit.kit import DProvenanceKit
from dprovenancekit.store import InMemoryTraceStore

pytest.importorskip("langchain_core")

from dprovenancekit.integrations.crewai import CrewAITracer


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
    def agent_start(cls, agent, inp):
        return cls("agent_start", 1, f'{{"agent": "{agent}", "inp": "{inp}"}}')

    @classmethod
    def agent_end(cls, agent, out):
        return cls("agent_end", 1, f'{{"agent": "{agent}", "out": "{out}"}}')


def test_crewai_tracer():
    kit = DProvenanceKit(APIEvent)
    store = InMemoryTraceStore()

    tracer = CrewAITracer(kit, APIEvent.agent_start, APIEvent.agent_end)

    with kit.run("crewai-run", store=store) as run:
        tracer.on_chain_start(
            serialized={},
            inputs={"task": "research"},
            run_id=uuid.uuid4(),
            metadata={"agent_role": "Researcher"},
        )

        tracer.on_chain_end(
            outputs={"result": "found info"},
            run_id=uuid.uuid4(),
        )

    run_snapshot = store.get_run(run.run_id)
    assert run_snapshot is not None
    events = run_snapshot.events
    assert len(events) == 2

    assert events[0].payload.type_identifier == "agent_start"
    assert events[0].engine_name == "Researcher"
    assert "Researcher" in events[0].payload.to_dict()["j"]

    assert events[1].payload.type_identifier == "agent_end"
    assert "found info" in events[1].payload.to_dict()["j"]

"""Tests for the CrewAI integration.

Modern CrewAI drives a :class:`~crewai.events.BaseEventListener` by emitting typed events
on a global bus. Two layers are tested:

* **Unit** — the translation logic is driven directly via
  ``DProvenanceKitEventListener.handle(kind, phase, source, event)`` with small stand-in
  objects carrying the same fields CrewAI's events carry (``emission_sequence``,
  ``event_id``, ``parent_event_id``, ``started_event_id``, …). No ``crewai`` needed, so
  these run everywhere. They also exercise the property that matters most under CrewAI's
  threaded bus: events delivered *out of order* still record a correctly-ordered,
  correctly-linked run.
* **Integration** — a real ``crew.kickoff()`` against an installed ``crewai`` (skipped
  otherwise), with the model call stubbed so no network/key is needed. This proves the
  listener actually binds to the real event bus and records a real crew.
"""

from __future__ import annotations

import pytest

from dprovenancekit import InMemoryTraceStore, TracePriority
from dprovenancekit.edge import TraceEdgeType
from dprovenancekit.integrations.crewai import (
    CrewAITraceEvent,
    DProvenanceKitEventListener,
)


@pytest.fixture(autouse=True)
def _isolate_crewai_bus():
    """Remove this adapter's handlers from the global bus after each test.

    A listener registers on ``crewai_event_bus`` when constructed (only if ``crewai`` is
    installed). Stripping our handlers afterwards keeps tests from accumulating listeners
    on the shared global bus; a no-op when ``crewai`` is absent.
    """
    yield
    try:
        from crewai.events import crewai_event_bus as bus

        for event_type, handlers in list(bus._sync_handlers.items()):
            bus._sync_handlers[event_type] = frozenset(
                h
                for h in handlers
                if getattr(h, "__module__", "") != "dprovenancekit.integrations.crewai"
            )
    except Exception:  # noqa: BLE001 - crewai not installed / internal shape changed
        pass


# ── Stand-ins for CrewAI's event / agent / task / crew objects ──────────────────


class FakeEvent:
    def __init__(self, **fields):
        self.__dict__.update(fields)


class FakeAgent:
    def __init__(self, role, goal=None, tools=None):
        self.role = role
        self.goal = goal
        self.tools = tools


class FakeTask:
    def __init__(self, name, description=None, agent=None):
        self.name = name
        self.description = description
        self.agent = agent


class FakeCrew:
    def __init__(self, name):
        self.name = name


def _events_by_type(run):
    return {e.payload.type_identifier: e for e in run.events}


def _kickoff_events(crew, agent, task):
    """The (kind, phase, source, event) tuples a real single-agent kickoff emits.

    Correlation fields mirror CrewAI's: a completion's ``started_event_id`` equals its
    start's ``event_id``, and ``parent_event_id`` nests the tree. ``emission_sequence`` is
    the authoritative order (deliberately gappy, as CrewAI's global counter is).
    """
    return [
        (
            "crew",
            "start",
            crew,
            FakeEvent(
                event_id="c",
                parent_event_id=None,
                emission_sequence=1,
                crew=crew,
                crew_name="research-crew",
                inputs={"topic": "ai"},
            ),
        ),
        (
            "task",
            "start",
            task,
            FakeEvent(
                event_id="t",
                parent_event_id="c",
                emission_sequence=2,
                task=task,
                task_name="research",
                agent_role=None,
            ),
        ),
        (
            "agent",
            "start",
            agent,
            FakeEvent(
                event_id="a",
                parent_event_id="t",
                emission_sequence=3,
                agent=agent,
                agent_role="Researcher",
                tools=["search"],
                task_prompt="Summarize AI",
            ),
        ),
        (
            "llm",
            "start",
            agent,
            FakeEvent(
                event_id="l1",
                parent_event_id="a",
                emission_sequence=4,
                model="gpt-4o",
                agent_role="Researcher",
                messages=[{"role": "user", "content": "hi"}],
                tools=[],
            ),
        ),
        (
            "llm",
            "end",
            agent,
            FakeEvent(
                event_id="l2",
                parent_event_id="a",
                started_event_id="l1",
                emission_sequence=5,
                model="gpt-4o",
                agent_role="Researcher",
                response="ANSWER",
                usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            ),
        ),
        (
            "tool",
            "start",
            None,
            FakeEvent(
                event_id="o1",
                parent_event_id="a",
                emission_sequence=6,
                tool_name="search",
                agent_role="Researcher",
                tool_args={"q": "ai"},
            ),
        ),
        (
            "tool",
            "end",
            None,
            FakeEvent(
                event_id="o2",
                parent_event_id="a",
                started_event_id="o1",
                emission_sequence=7,
                tool_name="search",
                agent_role="Researcher",
                output="3 results",
                from_cache=False,
            ),
        ),
        (
            "agent",
            "end",
            agent,
            FakeEvent(
                event_id="a2",
                parent_event_id="t",
                started_event_id="a",
                emission_sequence=8,
                agent=agent,
                agent_role="Researcher",
                output="answer",
            ),
        ),
        (
            "task",
            "end",
            task,
            FakeEvent(
                event_id="t2",
                parent_event_id="c",
                started_event_id="t",
                emission_sequence=9,
                task=task,
                task_name="research",
                agent_role="Researcher",
                output="answer",
            ),
        ),
        (
            "crew",
            "end",
            crew,
            FakeEvent(
                event_id="c2",
                parent_event_id=None,
                started_event_id="c",
                emission_sequence=10,
                crew=crew,
                crew_name="research-crew",
                output="final report",
                total_tokens=42,
            ),
        ),
    ]


def _drive(listener, events):
    for kind, phase, source, event in events:
        listener.handle(kind, phase, source, event)


# ── Event type ──────────────────────────────────────────────────────────────────


def test_event_roundtrip_and_canonical_encoding():
    ev = CrewAITraceEvent.make(
        "tool.end", TracePriority.STRUCTURAL, {"tool": "search", "output": "r"}
    )
    assert ev.type_identifier == "tool.end"
    assert ev.priority is TracePriority.STRUCTURAL
    assert (
        ev.encode().decode()
        == '{"output": "r", "priority": 2, "tool": "search", "type": "tool.end"}'
    )
    assert CrewAITraceEvent.decode(ev.encode()) == ev


# ── Full kickoff → run + span tree + engines ────────────────────────────────────


def test_records_full_kickoff():
    store = InMemoryTraceStore()
    listener = DProvenanceKitEventListener(store)
    crew = FakeCrew("research-crew")
    agent = FakeAgent("Researcher", goal="find info", tools=["search"])
    task = FakeTask("research", description="Summarize AI", agent=agent)

    _drive(listener, _kickoff_events(crew, agent, task))

    run = store.get_run(listener.run_id_for(crew))
    assert run.context_id == "research-crew"
    assert [e.payload.type_identifier for e in run.events] == [
        "crew.start",
        "task.start",
        "agent.start",
        "llm.start",
        "llm.end",
        "tool.start",
        "tool.end",
        "agent.end",
        "task.end",
        "crew.end",
    ]

    by_type = _events_by_type(run)
    # Engine = the active component (crew name / agent role / model / tool name).
    assert by_type["crew.start"].engine_name == "research-crew"
    assert by_type["task.start"].engine_name == "Researcher"  # via the task's agent
    assert by_type["agent.start"].engine_name == "Researcher"
    assert by_type["llm.start"].engine_name == "gpt-4o"
    assert by_type["tool.start"].engine_name == "search"
    # Start and completion of one operation share a span; the tree nests by parent.
    assert by_type["crew.start"].span_id == by_type["crew.end"].span_id
    assert by_type["crew.start"].parent_span_id is None
    assert by_type["task.start"].parent_span_id == by_type["crew.start"].span_id
    assert by_type["agent.start"].parent_span_id == by_type["task.start"].span_id
    assert by_type["llm.start"].parent_span_id == by_type["agent.start"].span_id
    assert by_type["llm.start"].span_id == by_type["llm.end"].span_id
    # Structural metadata is captured.
    assert by_type["agent.start"].payload.attributes["tools"] == ["search"]
    assert by_type["llm.end"].payload.attributes["total_tokens"] == 15
    assert by_type["crew.end"].payload.attributes["total_tokens"] == 42


def test_out_of_order_delivery_is_reordered_by_emission_sequence():
    """CrewAI's bus is threaded, so a completion can arrive before its start. The run must
    still come out ordered by emission_sequence, with start/end still paired."""
    store = InMemoryTraceStore()
    listener = DProvenanceKitEventListener(store)
    crew = FakeCrew("research-crew")
    agent = FakeAgent("Researcher", tools=["search"])
    task = FakeTask("research", agent=agent)
    events = _kickoff_events(crew, agent, task)

    # Scramble delivery: swap each start/end pair, and deliver crew.end before the
    # task/agent completions (as a busy 10-worker pool can).
    scrambled = [
        events[0],  # crew.start
        events[1],  # task.start
        events[2],  # agent.start
        events[4],  # llm.end   (before llm.start)
        events[3],  # llm.start
        events[6],  # tool.end  (before tool.start)
        events[5],  # tool.start
        events[9],  # crew.end  (before agent.end / task.end)
        events[7],  # agent.end
        events[8],  # task.end
    ]
    run_id = None
    for kind, phase, source, event in scrambled:
        listener.handle(kind, phase, source, event)
        if run_id is None:
            run_id = listener.run_id_for(crew)

    run = store.get_run(run_id)
    # Sequence order (the authoritative clock) is correct despite scrambled arrival.
    assert [e.payload.type_identifier for e in run.events] == [
        "crew.start",
        "task.start",
        "agent.start",
        "llm.start",
        "llm.end",
        "tool.start",
        "tool.end",
        "agent.end",
        "task.end",
        "crew.end",
    ]
    assert [e.sequence for e in run.events] == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]

    # DERIVED_FROM start→end still linked even though the ends arrived first.
    by_type = _events_by_type(run)
    for kind in ("crew", "task", "agent", "llm", "tool"):
        start, end = by_type[f"{kind}.start"], by_type[f"{kind}.end"]
        assert start.span_id == end.span_id
        incoming = {(e.source_id, e.type) for e in store.lineage_edges(end.id)}
        assert (start.id, TraceEdgeType.DERIVED_FROM) in incoming


# ── Lineage edges ────────────────────────────────────────────────────────────────


def test_lifecycle_edges_link_completion_to_start_and_child_to_parent():
    store = InMemoryTraceStore()
    listener = DProvenanceKitEventListener(store)
    crew = FakeCrew("research-crew")
    agent = FakeAgent("Researcher", tools=["search"])
    task = FakeTask("research", agent=agent)
    _drive(listener, _kickoff_events(crew, agent, task))

    by_type = _events_by_type(store.get_run(listener.run_id_for(crew)))
    # DERIVED_FROM: llm.start -> llm.end
    llm_incoming = {
        (e.source_id, e.type) for e in store.lineage_edges(by_type["llm.end"].id)
    }
    assert (by_type["llm.start"].id, TraceEdgeType.DERIVED_FROM) in llm_incoming
    # INFORMED: parent's start -> child's start, along crew -> task -> agent -> llm/tool.
    for parent, child in (
        ("crew", "task"),
        ("task", "agent"),
        ("agent", "llm"),
        ("agent", "tool"),
    ):
        incoming = {
            (e.source_id, e.type)
            for e in store.lineage_edges(by_type[f"{child}.start"].id)
        }
        assert (by_type[f"{parent}.start"].id, TraceEdgeType.INFORMED) in incoming


def test_link_lifecycle_off_produces_no_edges():
    store = InMemoryTraceStore()
    listener = DProvenanceKitEventListener(store, link_lifecycle=False)
    crew = FakeCrew("research-crew")
    agent = FakeAgent("Researcher")
    task = FakeTask("research", agent=agent)
    _drive(listener, _kickoff_events(crew, agent, task))

    run = store.get_run(listener.run_id_for(crew))
    assert len(run.events) == 10
    assert all(store.lineage_edges(e.id) == [] for e in run.events)


# ── Options / errors ─────────────────────────────────────────────────────────────


def test_capture_payloads_off_omits_io_keeps_structure():
    store = InMemoryTraceStore()
    listener = DProvenanceKitEventListener(store, capture_payloads=False)
    crew = FakeCrew("research-crew")
    agent = FakeAgent("Researcher", tools=["search"])
    task = FakeTask("research", description="Summarize AI", agent=agent)
    _drive(listener, _kickoff_events(crew, agent, task))

    by_type = _events_by_type(store.get_run(listener.run_id_for(crew)))
    # IO previews dropped ...
    assert "inputs" not in by_type["crew.start"].payload.attributes
    assert "output" not in by_type["crew.end"].payload.attributes
    assert "args" not in by_type["tool.start"].payload.attributes
    assert "output" not in by_type["tool.end"].payload.attributes
    assert "response" not in by_type["llm.end"].payload.attributes
    assert "description" not in by_type["task.start"].payload.attributes
    # ... structural metadata kept.
    assert by_type["agent.start"].payload.attributes["role"] == "Researcher"
    assert by_type["tool.start"].payload.attributes["tool"] == "search"
    assert by_type["llm.end"].payload.attributes["total_tokens"] == 15


def test_error_event_is_recorded_as_critical():
    store = InMemoryTraceStore()
    listener = DProvenanceKitEventListener(store)
    crew = FakeCrew("research-crew")
    listener.handle(
        "crew",
        "start",
        crew,
        FakeEvent(
            event_id="c",
            emission_sequence=1,
            crew=crew,
            crew_name="research-crew",
            inputs={},
        ),
    )
    listener.handle(
        "tool",
        "error",
        None,
        FakeEvent(
            event_id="e1",
            parent_event_id="c",
            emission_sequence=2,
            tool_name="search",
            agent_role="Researcher",
            error="tool exploded",
        ),
    )
    run_id = listener.run_id_for(crew)
    listener.handle(
        "crew",
        "error",
        crew,
        FakeEvent(
            event_id="c2",
            started_event_id="c",
            emission_sequence=3,
            crew=crew,
            crew_name="research-crew",
            error="crew failed",
        ),
    )

    by_type = _events_by_type(store.get_run(run_id))
    assert by_type["tool.error"].payload.priority is TracePriority.CRITICAL
    assert by_type["tool.error"].payload.attributes["error"] == "tool exploded"
    assert by_type["crew.error"].payload.priority is TracePriority.CRITICAL
    assert by_type["crew.error"].payload.attributes["error"] == "crew failed"


def test_sub_event_without_a_run_is_ignored():
    store = InMemoryTraceStore()
    listener = DProvenanceKitEventListener(store)
    # A stray sub-event before any kickoff: soft no-op, no crash, nothing recorded.
    listener.handle(
        "llm",
        "start",
        None,
        FakeEvent(event_id="l1", emission_sequence=1, model="gpt-4o"),
    )
    assert store._events_by_run == {}


def test_crew_reuse_starts_a_fresh_run():
    store = InMemoryTraceStore()
    listener = DProvenanceKitEventListener(store)
    crew = FakeCrew("research-crew")
    agent = FakeAgent("Researcher")
    task = FakeTask("research", agent=agent)

    _drive(listener, _kickoff_events(crew, agent, task))
    first = listener.run_id_for(crew)
    _drive(listener, _kickoff_events(crew, agent, task))
    second = listener.run_id_for(crew)

    assert first != second  # a second kickoff of the same crew is a separate run
    assert len(store._events_by_run) == 2
    assert len(store.get_run(first).events) == 10
    assert len(store.get_run(second).events) == 10


# ── Real CrewAI integration ──────────────────────────────────────────────────────


def test_real_crew_kickoff_records_a_run(monkeypatch, tmp_path):
    """A real ``crew.kickoff()`` against installed CrewAI, model call stubbed (offline)."""
    pytest.importorskip("crewai")

    monkeypatch.setenv("OPENAI_API_KEY", "sk-mock-not-used")
    monkeypatch.setenv("CREWAI_DISABLE_TELEMETRY", "true")
    monkeypatch.setenv("CREWAI_TRACING_ENABLED", "false")

    # Stub the OpenAI chat-completions call so no network/key is needed.
    from types import SimpleNamespace

    def _fake_create(self, *args, **kwargs):
        msg = SimpleNamespace(
            content="Hello from the mocked model.",
            role="assistant",
            tool_calls=None,
            function_call=None,
            refusal=None,
        )
        choice = SimpleNamespace(message=msg, finish_reason="stop", index=0)
        usage = SimpleNamespace(
            prompt_tokens=11,
            completion_tokens=7,
            total_tokens=18,
            model_dump=lambda: {
                "prompt_tokens": 11,
                "completion_tokens": 7,
                "total_tokens": 18,
            },
        )
        return SimpleNamespace(
            choices=[choice],
            usage=usage,
            id="chatcmpl-mock",
            model="gpt-4o",
            object="chat.completion",
            created=0,
            model_dump=lambda: {"id": "chatcmpl-mock"},
        )

    monkeypatch.setattr(
        "openai.resources.chat.completions.Completions.create",
        _fake_create,
        raising=True,
    )

    from crewai import Agent, Crew, Task, LLM
    from dprovenancekit import SQLiteTraceStore, TraceQueryDSL
    from dprovenancekit.testing import run_fingerprint

    store = SQLiteTraceStore(CrewAITraceEvent, str(tmp_path / "traces.sqlite"))
    listener = DProvenanceKitEventListener(store)

    llm = LLM(model="gpt-4o")
    researcher = Agent(
        role="Researcher",
        goal="Find accurate information",
        backstory="A meticulous researcher",
        llm=llm,
        verbose=False,
        max_iter=2,
    )
    task = Task(
        description="Summarize the latest in AI agent testing",
        expected_output="A short summary",
        agent=researcher,
    )
    crew = Crew(agents=[researcher], tasks=[task], name="research-crew", verbose=False)

    crew.kickoff()
    listener.force_flush()

    runs = store.query_runs(TraceQueryDSL())
    assert len(runs) == 1
    run = runs[0]
    assert run.context_id == "research-crew"

    types = [e.payload.type_identifier for e in run.events]
    for required in (
        "crew.start",
        "crew.end",
        "task.start",
        "task.end",
        "agent.start",
        "agent.end",
    ):
        assert required in types, f"missing {required} in {types}"
    assert any(t.startswith("llm.") for t in types)

    # The recorded run is ordered by CrewAI's emission_sequence, so its structural
    # fingerprint is stable and starts with the kickoff.
    assert run.events[0].payload.type_identifier == "crew.start"
    assert run.events[-1].payload.type_identifier == "crew.end"
    assert {e.engine_name for e in run.events} >= {"research-crew", "Researcher"}
    assert isinstance(run_fingerprint(run), str)

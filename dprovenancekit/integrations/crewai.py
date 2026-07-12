"""CrewAI integration — turn a crew kickoff into a trace.

Modern CrewAI (>= 0.85.0, which dropped its LangChain dependency in December 2024)
emits its own structured lifecycle events through a global event bus
(``crewai.events`` / ``crewai_event_bus``). A :class:`~crewai.events.BaseEventListener`
registers handlers on that bus; each handler receives ``(source, event)`` where ``event``
is a typed pydantic model (``CrewKickoffStartedEvent``, ``AgentExecutionStartedEvent``,
``TaskCompletedEvent``, ``ToolUsageFinishedEvent``, ``LLMCallStartedEvent``, …).
:class:`DProvenanceKitEventListener` translates that stream into DProvenanceKit runs:

* each crew kickoff becomes a run (``context_id`` = the crew's name);
* each lifecycle event becomes a typed :class:`CrewAITraceEvent` recorded in order —
  ``"<kind>.start"`` / ``".end"`` / ``".error"`` where *kind* is ``crew``, ``task``,
  ``agent``, ``tool``, or ``llm`` (e.g. ``agent.start``, ``tool.end``, ``llm.error``);
* the active component — the agent's role, the tool's name, the model — becomes the
  **engine**;
* the start and completion of one operation share a **span**, nested under their parent
  operation's span, forming the run's span tree;
* with ``link_lifecycle`` (default on), each completion is ``DERIVED_FROM`` its start and
  each child span is ``INFORMED`` by its parent.

Because everything flows through the normal recording path, the whole toolkit applies:
query the run, diff two runs, compare run **fingerprints** to detect a structurally
different crew path (a dropped task, a skipped tool, a looping agent), or align two runs
to grade a regression. Event attributes (inputs, outputs, token counts) never affect the
fingerprint — that is computed from ``type``/``engine`` in sequence order — so two runs
with the same crew structure but different tool outputs still compare equal structurally.

Ordering under CrewAI's threaded bus. CrewAI dispatches event handlers on a thread pool,
so handlers do **not** run in emission order — a completion can reach us before its start.
The translation therefore does not trust arrival order: it takes each event's authoritative
``emission_sequence`` (assigned synchronously when the event is emitted) as the recorded
sequence, pairs a start with its completion by CrewAI's own ``event_id`` /
``started_event_id`` correlation, and nests spans by ``parent_event_id``. The recorded run
is thus deterministic and its fingerprint stable across runs, which is what makes the
regression gate meaningful.

The listener registers itself on the global bus the moment it is constructed, so
instantiating it once is enough to record every subsequent ``crew.kickoff()``::

    from dprovenancekit import SQLiteTraceStore
    from dprovenancekit.integrations.crewai import (
        CrewAITraceEvent, DProvenanceKitEventListener,
    )

    store = SQLiteTraceStore(CrewAITraceEvent, "traces.sqlite")
    listener = DProvenanceKitEventListener(store)   # registers on crewai_event_bus

    # ... build and run your crew normally; each kickoff is recorded ...
    listener.force_flush()

Only *constructing* the listener needs ``crewai`` installed
(``pip install dprovenancekit[crewai]``). The translation logic imports nothing from
``crewai`` — it reads attributes off the event objects defensively — so it can be
unit-tested by driving :meth:`DProvenanceKitEventListener.handle` with stand-in objects.

Concurrency note: CrewAI's tool/LLM sub-events do not carry the owning crew's identity, so
a sub-event is attributed to the innermost crew kickoff currently open (a stack), or — for
a straggler that arrives just after its kickoff completed — to that most-recent run. This
is exact for the normal sequential case and for nested crews (a tool that kicks off a
sub-crew). Two crews kicked off *in parallel on separate threads* share one global bus
with no correlation id; record such crews in separate processes if exact isolation is
required.
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Set

from ..edge import TraceEdgeType
from ..event import TraceableEvent, TraceEvent
from ..priority import TracePriority

# Subclass CrewAI's ``BaseEventListener`` when installed so we are a first-class listener
# whose construction registers handlers on the global bus; fall back to ``object``
# otherwise so the translation logic stays importable and unit-testable without the
# dependency. ``BaseEventListener.__init__`` calls ``setup_listeners(crewai_event_bus)``;
# when the base is ``object`` that never runs, so no registration (and no crewai import)
# happens off the constructor.
try:  # pragma: no cover - import side-effect, exercised across envs
    from crewai.events import BaseEventListener as _BaseEventListener

    _HAS_CREWAI = True
except Exception:  # noqa: BLE001
    _BaseEventListener = object  # type: ignore[assignment,misc]
    _HAS_CREWAI = False


# ── Event type ─────────────────────────────────────────────────────────────────


def _jsonable(obj: Any) -> Any:
    return str(obj)


@dataclass(frozen=True)
class CrewAITraceEvent(TraceableEvent):
    """A CrewAI lifecycle event.

    Parallel to ``integrations.openai_agents.OpenAIAgentsTraceEvent``: attributes are
    stored as a canonical (sorted-key) JSON string so the event is hashable and two events
    with the same logical attributes compare equal (which makes exact-equality alignment
    work).
    """

    type_name: str
    priority_value: int
    attributes_json: str = "{}"

    @classmethod
    def make(
        cls,
        type_name: str,
        priority: TracePriority,
        attributes: Optional[Mapping[str, Any]] = None,
    ) -> "CrewAITraceEvent":
        clean = {k: v for k, v in (attributes or {}).items() if v is not None}
        return cls(
            type_name=type_name,
            priority_value=int(priority),
            attributes_json=json.dumps(clean, sort_keys=True, default=_jsonable),
        )

    @property
    def type_identifier(self) -> str:
        return self.type_name

    @property
    def priority(self) -> TracePriority:
        try:
            return TracePriority(self.priority_value)
        except ValueError:
            return TracePriority.TELEMETRY

    @property
    def attributes(self) -> Dict[str, Any]:
        return json.loads(self.attributes_json)

    def to_dict(self) -> dict:
        out: Dict[str, Any] = {"type": self.type_name, "priority": self.priority_value}
        out.update(json.loads(self.attributes_json))
        return out

    @classmethod
    def from_dict(cls, data: dict) -> "CrewAITraceEvent":
        attrs = {k: v for k, v in data.items() if k not in ("type", "priority")}
        return cls.make(
            type_name=data["type"],
            priority=TracePriority(
                int(data.get("priority", int(TracePriority.STRUCTURAL)))
            ),
            attributes=attrs,
        )


# ── Event → (kind, phase) binding ────────────────────────────────────────────────

# CrewAI event class name -> (kind, phase). Bound to the real classes in
# ``setup_listeners``; also the vocabulary the unit tests drive ``handle`` with. ``phase``
# is one of "start" / "end" / "error".
_EVENT_SPEC = (
    ("CrewKickoffStartedEvent", "crew", "start"),
    ("CrewKickoffCompletedEvent", "crew", "end"),
    ("CrewKickoffFailedEvent", "crew", "error"),
    ("TaskStartedEvent", "task", "start"),
    ("TaskCompletedEvent", "task", "end"),
    ("TaskFailedEvent", "task", "error"),
    ("AgentExecutionStartedEvent", "agent", "start"),
    ("AgentExecutionCompletedEvent", "agent", "end"),
    ("AgentExecutionErrorEvent", "agent", "error"),
    ("ToolUsageStartedEvent", "tool", "start"),
    ("ToolUsageFinishedEvent", "tool", "end"),
    ("ToolUsageErrorEvent", "tool", "error"),
    ("LLMCallStartedEvent", "llm", "start"),
    ("LLMCallCompletedEvent", "llm", "end"),
    ("LLMCallFailedEvent", "llm", "error"),
)


# ── Extraction helpers (defensive: field names vary by event and CrewAI version) ──


def _truncate(text: str, limit: int = 2000) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def _first(*values: Any) -> Optional[Any]:
    """First value that is not ``None`` and not an empty string."""
    for v in values:
        if v is not None and v != "":
            return v
    return None


def _str_or_none(value: Any) -> Optional[str]:
    return str(value) if value is not None else None


def _tool_names(items: Any) -> Optional[List[str]]:
    """Best-effort list of names from a list of tools (strings or tool objects)."""
    if not isinstance(items, (list, tuple)):
        return None
    out: List[str] = []
    for it in items:
        name = getattr(it, "name", None)
        out.append(str(name) if name is not None else str(it))
    return out


def _usage_attrs(usage: Any) -> Dict[str, Any]:
    """Pull token counts off a usage object/mapping across CrewAI/LiteLLM shapes."""
    out: Dict[str, Any] = {}
    keys = (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "input_tokens",
        "output_tokens",
    )
    if isinstance(usage, Mapping):
        for key in keys:
            if usage.get(key) is not None:
                out[key] = usage[key]
    elif usage is not None:
        for key in keys:
            val = getattr(usage, key, None)
            if val is not None:
                out[key] = val
    return out


def _crew_name(source: Any, event: Any) -> Optional[str]:
    return _first(
        getattr(event, "crew_name", None),
        getattr(getattr(event, "crew", None), "name", None),
        getattr(source, "name", None),
    )


def _agent_role(event: Any, source: Any) -> Optional[str]:
    role = getattr(event, "agent_role", None)
    if not role:
        role = getattr(getattr(event, "agent", None), "role", None)
    if not role:
        # Task events carry the executing agent on the task, not on the event itself.
        task = getattr(event, "task", None) or source
        role = getattr(getattr(task, "agent", None), "role", None)
    if not role:
        role = getattr(source, "role", None)
    return str(role) if role else None


def _engine_for(kind: str, source: Any, event: Any) -> str:
    """The active component's name — the trace's engine for this event."""
    if kind == "crew":
        return str(_crew_name(source, event) or "crew")
    if kind == "tool":
        return str(_first(getattr(event, "tool_name", None)) or "tool")
    if kind == "llm":
        return str(
            _first(getattr(event, "model", None), getattr(source, "model", None))
            or "llm"
        )
    # agent + task both run under the executing agent's role.
    return _agent_role(event, source) or ("task" if kind == "task" else "agent")


def _start_attributes(
    kind: str, source: Any, event: Any, capture: bool
) -> Dict[str, Any]:
    attrs: Dict[str, Any] = {}
    if kind == "crew":
        attrs["crew"] = _crew_name(source, event)
        if capture:
            inputs = getattr(event, "inputs", None)
            if inputs is not None:
                attrs["inputs"] = _truncate(str(inputs))
    elif kind == "task":
        attrs["task"] = _first(
            getattr(event, "task_name", None),
            getattr(getattr(event, "task", None), "name", None),
        )
        attrs["agent"] = _agent_role(event, source)
        if capture:
            desc = _first(
                getattr(getattr(event, "task", None), "description", None),
                getattr(event, "task_prompt", None),
            )
            if desc is not None:
                attrs["description"] = _truncate(str(desc))
    elif kind == "agent":
        attrs["role"] = _agent_role(event, source)
        attrs["tools"] = _tool_names(
            _first(getattr(event, "tools", None), getattr(source, "tools", None))
        )
        if capture:
            prompt = getattr(event, "task_prompt", None)
            if prompt is not None:
                attrs["task_prompt"] = _truncate(str(prompt))
    elif kind == "tool":
        attrs["tool"] = _first(getattr(event, "tool_name", None))
        attrs["agent"] = _agent_role(event, source)
        if capture:
            args = getattr(event, "tool_args", None)
            if args is not None:
                attrs["args"] = _truncate(str(args))
    elif kind == "llm":
        attrs["model"] = _first(getattr(event, "model", None))
        attrs["agent"] = _agent_role(event, source)
        messages = getattr(event, "messages", None)
        if isinstance(messages, (list, tuple)):
            # Count only — prompts are large and often sensitive, so drop the content.
            attrs["message_count"] = len(messages)
        tools = _tool_names(getattr(event, "tools", None))
        if tools is not None:
            attrs["tools"] = tools
    return attrs


def _end_attributes(
    kind: str, source: Any, event: Any, capture: bool
) -> Dict[str, Any]:
    attrs: Dict[str, Any] = {}
    if kind == "crew":
        attrs["crew"] = _crew_name(source, event)
        total = getattr(event, "total_tokens", None)
        if total is not None:
            attrs["total_tokens"] = total
        if capture:
            output = getattr(event, "output", None)
            if output is not None:
                attrs["output"] = _truncate(str(output))
    elif kind == "task":
        attrs["task"] = _first(
            getattr(event, "task_name", None),
            getattr(getattr(event, "task", None), "name", None),
        )
        attrs["agent"] = _agent_role(event, source)
        if capture:
            output = getattr(event, "output", None)
            if output is not None:
                attrs["output"] = _truncate(str(output))
    elif kind == "agent":
        attrs["role"] = _agent_role(event, source)
        if capture:
            output = getattr(event, "output", None)
            if output is not None:
                attrs["output"] = _truncate(str(output))
    elif kind == "tool":
        attrs["tool"] = _first(getattr(event, "tool_name", None))
        attrs["agent"] = _agent_role(event, source)
        from_cache = getattr(event, "from_cache", None)
        if from_cache is not None:
            attrs["from_cache"] = bool(from_cache)
        if capture:
            output = getattr(event, "output", None)
            if output is not None:
                attrs["output"] = _truncate(str(output))
    elif kind == "llm":
        attrs["model"] = _first(getattr(event, "model", None))
        attrs["agent"] = _agent_role(event, source)
        attrs.update(_usage_attrs(getattr(event, "usage", None)))
        if capture:
            response = getattr(event, "response", None)
            if response is not None:
                attrs["response"] = _truncate(str(response))
    return attrs


def _error_attributes(kind: str, source: Any, event: Any) -> Dict[str, Any]:
    attrs: Dict[str, Any] = {}
    if kind == "crew":
        attrs["crew"] = _crew_name(source, event)
    elif kind == "tool":
        attrs["tool"] = _first(getattr(event, "tool_name", None))
    elif kind == "llm":
        attrs["model"] = _first(getattr(event, "model", None))
    else:  # agent / task
        attrs["role"] = _agent_role(event, source)
    error = getattr(event, "error", None)
    if error is not None:
        attrs["error"] = _truncate(str(error))
    return attrs


# ── CrewAI event correlation (independent of arrival order) ──────────────────────


def _event_id(event: Any) -> Optional[str]:
    return _str_or_none(getattr(event, "event_id", None))


def _parent_event_id(event: Any) -> Optional[str]:
    return _str_or_none(getattr(event, "parent_event_id", None))


def _started_event_id(event: Any) -> Optional[str]:
    return _str_or_none(getattr(event, "started_event_id", None))


def _emission_sequence(event: Any) -> Optional[int]:
    seq = getattr(event, "emission_sequence", None)
    return seq if isinstance(seq, int) else None


def _crew_key(source: Any, event: Any) -> int:
    """Stable identity for a crew across its kickoff start and completion events."""
    crew = getattr(event, "crew", None)
    return id(crew) if crew is not None else id(source)


# ── Per-run bookkeeping ──────────────────────────────────────────────────────────


@dataclass
class _SpanRec:
    """Cross-arrival bookkeeping for one operation's span (start + completion).

    ``span_id`` is CrewAI's ``event_id`` of the operation's *start*; the completion carries
    it as ``started_event_id``, so start and end resolve to the same span whichever arrives
    first. ``parent`` is the parent operation's span id (``parent_event_id``).
    """

    start_id: Optional[uuid.UUID] = None
    end_id: Optional[uuid.UUID] = None
    parent: Optional[str] = None
    derived_linked: bool = False
    informed_linked: bool = False


@dataclass
class _RunState:
    """Per-kickoff bookkeeping: run identity plus span/edge resolution state."""

    run_id: uuid.UUID
    context_id: str
    key: int
    is_open: bool = True
    seq_fallback: int = 0
    spans: Dict[str, _SpanRec] = field(default_factory=dict)
    # parent span id -> child span ids whose INFORMED edge is waiting for the parent's
    # start to be recorded (the parent may arrive after the child under threaded delivery).
    pending_informed: Dict[str, Set[str]] = field(default_factory=dict)


# ── The listener ─────────────────────────────────────────────────────────────────


class DProvenanceKitEventListener(_BaseEventListener):  # type: ignore[misc,valid-type]
    """A CrewAI ``BaseEventListener`` that records each crew kickoff as a DProvenanceKit run.

    Constructing the listener registers its handlers on the global ``crewai_event_bus``
    (via ``BaseEventListener.__init__``), so one instance captures every crew kickoff that
    runs while it is alive. It is safe to share across threads: all per-run state lives in a
    :class:`_RunState` and a single lock guards the map of runs and the edge resolution.

    Options:
        capture_payloads: include IO previews (crew inputs/outputs, task/agent outputs,
            tool args/results, LLM responses) in event attributes. With it off, only
            structural metadata is kept (names, roles, models, token counts). LLM prompt
            *content* is never captured regardless — only the message count.
        link_lifecycle: emit provenance edges (``DERIVED_FROM`` start→end, ``INFORMED``
            parent→child).
    """

    def __init__(
        self,
        store: Any,
        *,
        schema_version: int = 1,
        capture_payloads: bool = True,
        link_lifecycle: bool = True,
    ) -> None:
        self._store = store
        self._schema_version = schema_version
        self._capture = capture_payloads
        self._link = link_lifecycle
        self._lock = threading.Lock()
        self._runs: Dict[int, _RunState] = {}  # id(crew) -> run state
        self._open_keys: List[int] = []  # innermost open kickoff last
        self._last_key: Optional[int] = None  # for stragglers after a kickoff completes
        # Registers handlers on crewai_event_bus when CrewAI is installed; a no-op
        # (object.__init__) otherwise, keeping construction dependency-free for tests.
        super().__init__()

    # MARK: - Registration -------------------------------------------------------

    def setup_listeners(self, crewai_event_bus: Any) -> None:
        """Bind a handler for each known CrewAI event class onto the bus.

        Called by ``BaseEventListener.__init__`` with the global bus. Event classes are
        imported here (not at module load) so the module stays importable without CrewAI.
        """
        from crewai.events import event_types as _types  # requires crewai

        for cls_name, kind, phase in _EVENT_SPEC:
            event_cls = getattr(_types, cls_name, None)
            if event_cls is None:
                continue  # tolerate classes a given CrewAI version does not ship
            self._bind(crewai_event_bus, event_cls, kind, phase)

    def _bind(self, bus: Any, event_cls: Any, kind: str, phase: str) -> None:
        listener = self

        # The handler MUST take exactly ``(source, event)``. CrewAI's bus inspects the
        # parameter count and, for any handler with >= 3 params, calls
        # ``handler(source, event, state)`` — so extra parameters (even with defaults) get
        # clobbered by its internal RuntimeState. ``kind``/``phase`` are captured from this
        # enclosing scope instead (a fresh scope per ``_bind`` call, so no loop aliasing).
        @bus.on(event_cls)
        def _handler(source: Any, event: Any) -> None:
            listener.handle(kind, phase, source, event)

    # MARK: - Translation (importable/testable without crewai) --------------------

    def handle(self, kind: str, phase: str, source: Any, event: Any) -> None:
        """Translate one CrewAI lifecycle event into a recorded trace event.

        This is the single translation entry point. Capture is failure-proof: any error
        translating an event is swallowed so instrumentation never breaks the crew.
        """
        try:
            with self._lock:
                self._handle_locked(kind, phase, source, event)
        except Exception:  # noqa: BLE001 - instrumentation must never break the crew
            pass

    def _handle_locked(self, kind: str, phase: str, source: Any, event: Any) -> None:
        if kind == "crew":
            key = _crew_key(source, event)
            if phase == "start":
                state = self._open_run(key, source, event)
                self._record(state, kind, "start", source, event)
                return
            close_state = self._runs.get(key) or self._active_run()
            if close_state is None:
                return
            self._record(close_state, kind, phase, source, event)
            self._close_run(key, close_state)
            return

        # Sub-event (task / agent / tool / llm): the innermost open kickoff, or — for a
        # straggler that arrives just after its kickoff completed — the most recent run.
        sub_state = self._active_run()
        if sub_state is None:
            return  # a sub-event with no run to attach to — soft no-op
        self._record(sub_state, kind, phase, source, event)

    def _active_run(self) -> Optional[_RunState]:
        if self._open_keys:
            return self._runs.get(self._open_keys[-1])
        if self._last_key is not None:
            return self._runs.get(self._last_key)
        return None

    def _open_run(self, key: int, source: Any, event: Any) -> _RunState:
        existing = self._runs.get(key)
        if existing is not None and existing.is_open:
            return existing  # duplicate kickoff-started for a live crew — keep the run
        state = _RunState(
            run_id=uuid.uuid4(),
            context_id=_engine_for("crew", source, event),
            key=key,
        )
        self._runs[key] = (
            state  # replaces a prior completed run (a crew reused for a new kickoff)
        )
        self._open_keys.append(key)
        self._last_key = key
        return state

    def _close_run(self, key: int, state: _RunState) -> None:
        state.is_open = False
        if key in self._open_keys:
            self._open_keys.remove(key)
        # Keep ``state`` in ``_runs`` so a straggling sub-event delivered after the crew
        # completes still lands in the right run; a later kickoff of the same crew replaces
        # it. Flush so the completed run is durable.
        try:
            self._store.flush()
        except Exception:  # noqa: BLE001 - flush is best-effort
            pass

    # MARK: - Recording ----------------------------------------------------------

    def _record(
        self, state: _RunState, kind: str, phase: str, source: Any, event: Any
    ) -> None:
        engine = _engine_for(kind, source, event)
        sequence = _emission_sequence(event)
        if sequence is None:
            state.seq_fallback += 1
            sequence = state.seq_fallback
        parent_span = _parent_event_id(event)

        if phase == "start":
            span_id = _event_id(event) or str(uuid.uuid4())
            attrs = _start_attributes(kind, source, event, self._capture)
            rec_id = self._emit(
                state,
                f"{kind}.start",
                TracePriority.STRUCTURAL,
                attrs,
                engine,
                sequence,
                span_id,
                parent_span,
            )
            rec = state.spans.setdefault(span_id, _SpanRec())
            rec.start_id = rec_id
            if parent_span is not None:
                rec.parent = parent_span
            self._resolve_edges(state, span_id)
            return

        # end / error: the completion resolves to its start's span via started_event_id, so
        # start and completion share a span whichever arrives first.
        span_id = _started_event_id(event) or _event_id(event) or str(uuid.uuid4())
        if phase == "error":
            type_name, priority = f"{kind}.error", TracePriority.CRITICAL
            attrs = _error_attributes(kind, source, event)
        else:
            type_name, priority = f"{kind}.end", TracePriority.STRUCTURAL
            attrs = _end_attributes(kind, source, event, self._capture)
        rec_id = self._emit(
            state,
            type_name,
            priority,
            attrs,
            engine,
            sequence,
            span_id,
            parent_span,
        )
        rec = state.spans.setdefault(span_id, _SpanRec())
        rec.end_id = rec_id
        if rec.parent is None and parent_span is not None:
            rec.parent = parent_span
        self._resolve_edges(state, span_id)

    def _resolve_edges(self, state: _RunState, span_id: str) -> None:
        """Emit the DERIVED_FROM / INFORMED edges for ``span_id`` that are now resolvable.

        Order-independent: an edge is created as soon as *both* of its endpoints have been
        recorded, whichever event arrived first.
        """
        if not self._link:
            return
        rec = state.spans.get(span_id)
        if rec is None:
            return

        # DERIVED_FROM: this operation's start -> its completion.
        if (
            rec.start_id is not None
            and rec.end_id is not None
            and not rec.derived_linked
        ):
            self._link_edge(rec.start_id, rec.end_id, TraceEdgeType.DERIVED_FROM)
            rec.derived_linked = True

        # INFORMED: parent's start -> this operation's start.
        if (
            rec.start_id is not None
            and rec.parent is not None
            and not rec.informed_linked
        ):
            parent = state.spans.get(rec.parent)
            if parent is not None and parent.start_id is not None:
                self._link_edge(parent.start_id, rec.start_id, TraceEdgeType.INFORMED)
                rec.informed_linked = True
            else:
                # Parent not recorded yet — wait for it (or it may never come, e.g. an
                # internal CrewAI step we don't subscribe to).
                state.pending_informed.setdefault(rec.parent, set()).add(span_id)

        # This start may itself be the awaited parent of earlier-arrived children.
        if rec.start_id is not None:
            waiting = state.pending_informed.pop(span_id, None)
            if waiting:
                for child_id in waiting:
                    child = state.spans.get(child_id)
                    if (
                        child is not None
                        and child.start_id is not None
                        and not child.informed_linked
                    ):
                        self._link_edge(
                            rec.start_id, child.start_id, TraceEdgeType.INFORMED
                        )
                        child.informed_linked = True

    def _emit(
        self,
        state: _RunState,
        type_name: str,
        priority: TracePriority,
        attributes: Mapping[str, Any],
        engine: Optional[str],
        sequence: int,
        span_id: Optional[str],
        parent_span_id: Optional[str],
    ) -> uuid.UUID:
        """Record one event straight onto the store with an explicit ``sequence``.

        The sequence comes from CrewAI's ``emission_sequence``, not our arrival order, so
        the run is ordered deterministically regardless of the bus's threaded dispatch.
        """
        event = TraceEvent(
            run_id=state.run_id,
            context_id=state.context_id,
            engine_name=engine if engine is not None else "Unknown",
            schema_version=self._schema_version,
            sequence=sequence,
            span_id=span_id,
            parent_span_id=parent_span_id,
            payload=CrewAITraceEvent.make(type_name, priority, attributes),
        )
        self._store.record(event)
        return event.id

    def _link_edge(
        self, source: uuid.UUID, target: uuid.UUID, edge_type: TraceEdgeType
    ) -> None:
        if source == target:  # never valid provenance
            return
        self._store.link(source, target, edge_type)

    # MARK: - Introspection / flush ----------------------------------------------

    def run_id_for(self, crew: Any) -> Optional[uuid.UUID]:
        """The DProvenanceKit run id recording the given crew's most recent kickoff.

        Available during and after the kickoff (the run is retained until the crew is
        kicked off again), so it can be read after ``crew.kickoff()`` returns.
        """
        with self._lock:
            state = self._runs.get(id(crew))
        return state.run_id if state is not None else None

    def force_flush(self) -> None:
        """Wait for CrewAI's event bus, then durably flush the backing store.

        CrewAI dispatches handlers on a thread pool. In current releases the final
        ``CrewKickoffCompletedEvent`` is emitted after ``Crew.kickoff()`` performs its
        own bus flush, so it may still be in flight when ``kickoff()`` returns. Draining
        the bus here ensures the trailing ``crew.end`` event reaches this listener before
        the store is made durable.
        """
        if _HAS_CREWAI:
            try:
                from crewai.events import crewai_event_bus

                flush_bus = getattr(crewai_event_bus, "flush", None)
                if callable(flush_bus):
                    flush_bus()
            except Exception:  # noqa: BLE001 - upstream flush is best-effort
                pass
        try:
            self._store.flush()
        except Exception:  # noqa: BLE001 - flush is best-effort
            pass


__all__ = [
    "CrewAITraceEvent",
    "DProvenanceKitEventListener",
]

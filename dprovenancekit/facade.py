"""High-level facade API for DProvenanceKit.

Provides the ``trace`` global object for ergonomic, stateful tracing without manually
constructing stores, kits, or runs.

Example:
    from dprovenancekit import trace

    with trace("Agent Workflow"):
        with trace("Verify Claims"):
            ...

    trace.save("golden_run.sqlite")
    trace.diff("golden_run.sqlite")
"""

import json
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Union

from .context import TraceContext
from .diff import ChangeKind, TraceDiffEngine
from .edge import TraceEdgeType
from .event import AnyTraceableEvent, TraceEvent
from .instrument import _KIT, TracedEvent, _enclosing_step, _link, _record_in_span
from .priority import TracePriority
from .query import TraceRun
from .raw_store import RawTraceStore
from .sqlite_store import SQLiteTraceStore
from .store import InMemoryTraceStore


class _TraceFacade:
    """Global stateful tracing facade.

    Each *top-level* ``with trace(...)`` block implicitly starts (and ends) its own
    in-memory run; nested blocks join the enclosing one. ``save`` / ``explain`` /
    ``diff`` operate on the most recently completed top-level block.

    The facade is a process-global convenience for scripts and notebooks. It is not
    thread-safe: concurrent top-level blocks race on the last-run handle. For
    concurrent or production recording, use ``traced_run`` / ``DProvenanceKit.run``
    with an explicit store.
    """

    def __init__(self):
        self._default_store = InMemoryTraceStore()
        self._last_run_id: Optional[uuid.UUID] = None

    @contextmanager
    def __call__(self, name: str) -> Iterator[None]:
        """Trace a block of code. A top-level block implicitly starts a new run."""
        is_root = TraceContext.current_run.get() is None
        if is_root:
            run_ctx = _KIT.run(context_id="implicit-run", store=self._default_store)
            run = run_ctx.__enter__()
            self._last_run_id = run.run_id
        else:
            run_ctx = None

        span_id = str(uuid.uuid4())
        parent = TraceContext.current_span_id.get()

        start_id = _record_in_span(
            f"{name}.start",
            TracePriority.STRUCTURAL,
            {"name": name},
            engine=name,
            span_id=span_id,
            parent_span_id=parent,
        )
        _link(_enclosing_step.get(), start_id, TraceEdgeType.INFORMED)
        token = _enclosing_step.set(start_id)

        try:
            try:
                with _KIT.with_span(span_id), _KIT.with_engine(name):
                    yield
            except Exception as error:
                err_id = _record_in_span(
                    f"{name}.error",
                    TracePriority.CRITICAL,
                    {"name": name, "error": str(error)},
                    engine=name,
                    span_id=span_id,
                    parent_span_id=parent,
                )
                _link(start_id, err_id, TraceEdgeType.DERIVED_FROM)
                raise
            finally:
                _enclosing_step.reset(token)

            end_id = _record_in_span(
                f"{name}.end",
                TracePriority.STRUCTURAL,
                {"name": name},
                engine=name,
                span_id=span_id,
                parent_span_id=parent,
            )
            _link(start_id, end_id, TraceEdgeType.DERIVED_FROM)
        finally:
            if is_root and run_ctx:
                run_ctx.__exit__(None, None, None)

    def save(
        self, filepath: Union[str, Path], run_id: Optional[uuid.UUID] = None
    ) -> None:
        """Save a facade-recorded run to a ``.jsonl`` or ``.sqlite`` file.

        Defaults to the most recent top-level block's run. Only runs recorded through
        the facade's implicit store can be saved here; code that records to an explicit
        store already has the file (or the store) in hand.
        """
        target_run_id = run_id or self._last_run_id
        if target_run_id is None:
            raise RuntimeError("No trace run has been executed yet.")
        run = self._default_store.get_run(target_run_id)
        if run is None:
            raise RuntimeError(
                f"Run {target_run_id} was not recorded through the trace facade."
            )

        path = Path(filepath)
        if path.suffix == ".jsonl":
            with open(path, "w") as f:
                for event in run.events:
                    f.write(json.dumps(self._event_to_json(event)) + "\n")
        elif path.suffix == ".sqlite":
            with SQLiteTraceStore(event_type=TracedEvent, path=str(path)) as store:
                for event in run.events:
                    store.record(event)
                store.flush()
        else:
            raise ValueError("Unsupported file extension. Use .jsonl or .sqlite")

    def explain(self) -> None:
        """Print an indented walk of the most recent top-level run."""
        run = self._current_run()
        if run is None:
            print("No trace run found to explain.")
            return

        print(f"\n--- Execution Trace ({run.run_id}) ---")
        indent = 0
        for event in run.events:
            type_id = event.payload.type_identifier
            if type_id.endswith(".start"):
                print("  " * indent + f"▶ Started {event.engine_name}")
                indent += 1
            elif type_id.endswith(".end"):
                indent = max(0, indent - 1)
                print("  " * indent + f"✔ Finished {event.engine_name}")
            elif type_id.endswith(".error"):
                indent = max(0, indent - 1)
                print("  " * indent + f"✖ Error in {event.engine_name}")
            else:
                print("  " * indent + f"• {type_id} (Engine: {event.engine_name})")

    def diff(self, golden_filepath: Union[str, Path]) -> None:
        """Diff the most recent run against a saved *golden* (baseline) run.

        The file is the baseline: a step present in the golden run but absent from the
        current one prints as missing; a step only in the current run prints as added.
        """
        candidate = self._current_run()
        if candidate is None:
            print("No trace run found to diff.")
            return

        path = Path(golden_filepath)
        if not path.exists():
            print(f"Golden file {golden_filepath} not found.")
            return
        golden = self._load_run(path)
        if golden is None:
            print(f"Could not load a golden run from {golden_filepath}.")
            return

        result = TraceDiffEngine().diff(
            golden, candidate, minimum_priority=TracePriority.STRUCTURAL
        )
        print("\n--- Trace Diff (Golden vs Current) ---")
        if result.is_identical:
            print("✅ The execution paths are structurally identical.")
            return

        # A facade step is a `.start`/`.end` event pair sharing one engine name, so a
        # dropped step would otherwise print twice; collapse to one line per step.
        reported = set()
        for change in result.changes:
            type_id = change.type_identifier
            if type_id.endswith((".start", ".end", ".error")):
                noun, label = "step", change.engine_name
            else:
                noun, label = "event", type_id
            key = (change.kind, noun, label)
            if key in reported:
                continue
            reported.add(key)
            if change.kind == ChangeKind.REMOVED:
                print(f"❌ Missing {noun}: {label}")
            else:
                print(f"➕ Added {noun}: {label}")

    def _current_run(self) -> Optional[TraceRun]:
        if self._last_run_id is None:
            return None
        return self._default_store.get_run(self._last_run_id)

    @staticmethod
    def _event_to_json(event: TraceEvent) -> dict:
        return {
            "id": str(event.id),
            "run_id": str(event.run_id),
            "context_id": event.context_id,
            "sequence": event.sequence,
            "engine": event.engine_name,
            "span_id": event.span_id,
            "parent_span_id": event.parent_span_id,
            "type": event.payload.type_identifier,
            "priority": event.payload.priority.value,
            "timestamp": event.timestamp,
            "payload": event.payload.to_dict(),
        }

    @staticmethod
    def _event_from_json(raw: dict) -> TraceEvent:
        payload_json = raw.get("payload") or {}
        raw_json = payload_json.get("raw_json")
        # The top-level envelope fields are authoritative: a payload dict can carry
        # user attributes that shadow "type"/"priority", and identity must not be
        # rewritable by attribute names (see TracedEvent.to_dict).
        payload = AnyTraceableEvent(
            type_identifier_value=raw["type"],
            priority_value=raw["priority"],
            raw_json=raw_json
            if isinstance(raw_json, str)
            else json.dumps(payload_json, sort_keys=True),
        )
        return TraceEvent(
            id=uuid.UUID(raw["id"]),
            run_id=uuid.UUID(raw["run_id"]),
            context_id=raw["context_id"],
            engine_name=raw["engine"],
            schema_version=1,
            sequence=raw["sequence"],
            span_id=raw["span_id"],
            parent_span_id=raw["parent_span_id"],
            payload=payload,
            timestamp=raw["timestamp"],
        )

    @staticmethod
    def _load_run(path: Path) -> Optional[TraceRun]:
        """Load one run from a saved file — the newest, if the file holds several."""
        if path.suffix == ".sqlite":
            # Read through the raw viewer path: no writer thread, no schema writes,
            # and each event's identity/priority comes from the authoritative row
            # columns rather than the payload blob — so a golden written by any event
            # type (a framework adapter, another SDK) loads faithfully.
            with RawTraceStore(str(path)) as reader:
                runs = reader.fetch_all_runs()  # newest first
            if not runs or not runs[0].events:
                return None
            newest = runs[0]
            return TraceRun(
                run_id=newest.run_id,
                context_id=newest.context_id,
                events=[
                    TraceEvent(
                        id=e.id,
                        run_id=e.run_id,
                        context_id=e.context_id,
                        engine_name=e.engine_name,
                        schema_version=1,
                        sequence=e.sequence,
                        span_id=e.span_id,
                        parent_span_id=e.parent_span_id,
                        payload=AnyTraceableEvent(
                            type_identifier_value=e.type_identifier,
                            priority_value=e.priority,
                            raw_json=e.payload_json,
                        ),
                        timestamp=e.timestamp,
                    )
                    for e in newest.events
                ],
            )
        if path.suffix == ".jsonl":
            events: List[TraceEvent] = []
            with open(path) as f:
                for line in f:
                    if not line.strip():
                        continue
                    events.append(_TraceFacade._event_from_json(json.loads(line)))
            if not events:
                return None
            # A concatenated file can hold several runs, each with its own sequence
            # numbering — group by run and keep the newest, mirroring the sqlite path.
            by_run: Dict[uuid.UUID, List[TraceEvent]] = {}
            for event in events:
                by_run.setdefault(event.run_id, []).append(event)
            newest = max(by_run.values(), key=lambda evs: max(e.timestamp for e in evs))
            newest.sort(key=lambda e: e.sequence)
            return TraceRun(
                run_id=newest[0].run_id,
                context_id=newest[0].context_id,
                events=newest,
            )
        raise ValueError("Unsupported file extension. Use .jsonl or .sqlite")


# Singleton instance
trace = _TraceFacade()

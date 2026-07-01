"""High-level Facade API for DProvenanceKit.

Provides the ``trace`` global object which allows for ergonomic, stateful tracing
without needing to manually construct Stores, Kits, or Runs.

Example:
    from dprovenancekit import trace

    with trace("Summarize PDF"):
        text = "Extracted text"

    trace.save("run.jsonl")
"""

import os
import uuid
import json
from contextlib import contextmanager
from typing import Iterator, Optional, Dict, Any, Union
from pathlib import Path

from .context import TraceContext
from .store import InMemoryTraceStore
from .sqlite_store import SQLiteTraceStore
from .instrument import _KIT, _record_in_span, _enclosing_step, _link, TracedEvent
from .edge import TraceEdgeType
from .priority import TracePriority
from .query import TraceRun
from .diff import TraceDiffEngine


class _TraceFacade:
    """Global stateful tracing facade."""

    def __init__(self):
        self._default_store = InMemoryTraceStore()
        self._active_runs: Dict[str, Any] = {}
        self._last_run_id: Optional[uuid.UUID] = None

    @contextmanager
    def __call__(self, name: str) -> Iterator[None]:
        """Context manager to trace a block of code.

        If no run is active, implicitly starts a new run in memory.
        """
        is_root = TraceContext.current_run.get() is None
        
        # If no run is active, we implicitly start one
        if is_root:
            run_id_str = str(uuid.uuid4())
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
            if is_root and run_ctx:
                run_ctx.__exit__(None, None, None)

        end_id = _record_in_span(
            f"{name}.end",
            TracePriority.STRUCTURAL,
            {"name": name},
            engine=name,
            span_id=span_id,
            parent_span_id=parent,
        )
        _link(start_id, end_id, TraceEdgeType.DERIVED_FROM)

    def save(self, filepath: Union[str, Path], run_id: Optional[uuid.UUID] = None) -> None:
        """Saves the trace run to a file (.jsonl or .sqlite)."""
        target_run_id = run_id or self._last_run_id
        if not target_run_id:
            raise RuntimeError("No trace run has been executed yet.")

        # Fetch events from the implicit memory store
        events = self._default_store._events_by_run.get(target_run_id, [])
        
        path = Path(filepath)
        if path.suffix == ".jsonl":
            with open(path, "w") as f:
                for event in events:
                    if hasattr(event, "to_dict"):
                        data = event.to_dict()
                    else:
                        data = event
                    f.write(json.dumps({
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
                        "payload": event.payload.to_dict() if hasattr(event.payload, "to_dict") else {}
                    }) + "\n")
        elif path.suffix == ".sqlite":
            sqlite_store = SQLiteTraceStore(event_type=TracedEvent, path=str(path))
            for event in events:
                sqlite_store.record(event)
            sqlite_store.flush()
        else:
            raise ValueError("Unsupported file extension. Use .jsonl or .sqlite")

    def explain(self) -> None:
        """Provides a natural language explanation of the execution flow."""
        if not self._last_run_id:
            print("No trace run found to explain.")
            return

        events = self._default_store._events_by_run.get(self._last_run_id, [])
        print(f"\n--- Execution Trace ({self._last_run_id}) ---")
        
        indent = 0
        for event in sorted(events, key=lambda e: e.sequence):
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

    def diff(self, candidate_filepath: str) -> None:
        """Compares the current run against a saved candidate run."""
        if not self._last_run_id:
            print("No trace run found to diff.")
            return

        events_golden = self._default_store._events_by_run.get(self._last_run_id, [])
        if not events_golden:
            return
            
        golden_run = TraceRun(
            run_id=self._last_run_id,
            context_id=events_golden[0].context_id,
            events=events_golden
        )
        
        path = Path(candidate_filepath)
        if not path.exists():
            print(f"Candidate file {candidate_filepath} not found.")
            return
            
        candidate_events = []
        if path.suffix == ".sqlite":
            from .query import TraceQueryDSL
            temp_store = SQLiteTraceStore(event_type=TracedEvent, path=str(path))
            runs = temp_store.query_runs(TraceQueryDSL())
            if runs:
                candidate_events = runs[0].events
        elif path.suffix == ".jsonl":
            import json
            from .event import AnyTraceableEvent, TraceEvent
            with open(path, "r") as f:
                for line in f:
                    if not line.strip(): continue
                    raw = json.loads(line)
                    payload_json = raw["payload"]
                    type_id = payload_json.get("type_identifier_value", raw["type"])
                    prio = payload_json.get("priority_value", raw["priority"])
                    raw_json = payload_json.get("raw_json", json.dumps(payload_json))
                    
                    payload = AnyTraceableEvent(
                        type_identifier_value=type_id,
                        priority_value=prio,
                        raw_json=raw_json
                    )
                    candidate_events.append(TraceEvent(
                        id=uuid.UUID(raw["id"]),
                        run_id=uuid.UUID(raw["run_id"]),
                        context_id=raw["context_id"],
                        engine_name=raw["engine"],
                        schema_version=1,
                        sequence=raw["sequence"],
                        span_id=raw["span_id"],
                        parent_span_id=raw["parent_span_id"],
                        payload=payload,
                        timestamp=raw["timestamp"]
                    ))
        
        if not candidate_events:
            print(f"Could not load candidate run from {candidate_filepath}.")
            return
            
        candidate_run = TraceRun(
            run_id=candidate_events[0].run_id,
            context_id=candidate_events[0].context_id,
            events=candidate_events
        )
        
        engine = TraceDiffEngine()
        result = engine.diff(golden_run, candidate_run, minimum_priority=TracePriority.STRUCTURAL)
        
        print(f"\n--- Trace Diff (Golden vs {path.name}) ---")
        if result.is_identical:
            print("✅ The execution paths are structurally identical.")
        else:
            print("⚠️ Drift detected:")
            for change in result.changes:
                if change.kind.value == "added":
                    print(f"  [+] Added: {change.type_identifier} (Engine: {change.engine_name})")
                elif change.kind.value == "removed":
                    print(f"  [-] Missing: {change.type_identifier} (Engine: {change.engine_name})")


# Singleton instance
trace = _TraceFacade()

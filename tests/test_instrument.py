"""Tests for the framework-agnostic instrumentation layer (``dprovenancekit.instrument``)."""

from __future__ import annotations

import asyncio
import tempfile
import uuid

import pytest

from dprovenancekit import (
    InMemoryTraceStore,
    SQLiteTraceStore,
    TracePriority,
    TracedEvent,
    record_event,
    traced,
    traced_run,
)
from dprovenancekit.edge import TraceEdgeType


def _events_by_type(run):
    return {e.payload.type_identifier: e for e in run.events}


def _get_run(store, run):
    return store.get_run(run.run_id)


# ── Event type ──────────────────────────────────────────────────────────────────


def test_event_roundtrip_and_canonical_encoding():
    ev = TracedEvent.make(
        "search.end", TracePriority.STRUCTURAL, {"name": "search", "result": "3"}
    )
    assert ev.type_identifier == "search.end"
    assert (
        ev.encode().decode()
        == '{"name": "search", "priority": 2, "result": "3", "type": "search.end"}'
    )
    assert TracedEvent.decode(ev.encode()) == ev


# ── Basic decoration ─────────────────────────────────────────────────────────────


def test_traced_records_start_and_end_with_engine_and_span():
    store = InMemoryTraceStore()

    @traced
    def search(query):
        return [1, 2, 3]

    with traced_run(store, context_id="c") as run:
        search("kittens")

    rec = _get_run(store, run)
    types = [e.payload.type_identifier for e in rec.events]
    assert types == ["search.start", "search.end"]
    by_type = _events_by_type(rec)
    # function name is the engine; both events share the step's span.
    assert by_type["search.start"].engine_name == "search"
    assert by_type["search.start"].span_id == by_type["search.end"].span_id
    assert by_type["search.start"].span_id is not None
    # args / result captured.
    assert by_type["search.start"].payload.attributes["args"] == ["'kittens'"]
    assert by_type["search.end"].payload.attributes["result"] == "[1, 2, 3]"


def test_custom_name_and_capture_off():
    store = InMemoryTraceStore()

    @traced(name="retrieve", capture_args=False, capture_result=False)
    def f(secret):
        return "secret-output"

    with traced_run(store, context_id="c") as run:
        f("password")

    by_type = _events_by_type(_get_run(store, run))
    assert "retrieve.start" in by_type and "retrieve.end" in by_type
    assert "args" not in by_type["retrieve.start"].payload.attributes
    assert "result" not in by_type["retrieve.end"].payload.attributes


# ── Nesting / span tree / edges ──────────────────────────────────────────────────


def test_nested_steps_nest_in_span_tree_and_link():
    store = InMemoryTraceStore()

    @traced
    def inner(x):
        return x + 1

    @traced
    def outer(x):
        return inner(x) * 2

    with traced_run(store, context_id="c") as run:
        assert outer(10) == 22

    rec = _get_run(store, run)
    by_type = _events_by_type(rec)
    outer_start, inner_start = by_type["outer.start"], by_type["inner.start"]
    inner_end = by_type["inner.end"]
    # inner's span is a child of outer's span.
    assert inner_start.parent_span_id == outer_start.span_id
    assert inner_start.span_id != outer_start.span_id
    # Lineage edges: inner.end DERIVED_FROM inner.start; inner.start INFORMED by outer.start.
    incoming = {(e.source_id, e.type) for e in store.lineage_edges(inner_end.id)}
    assert (inner_start.id, TraceEdgeType.DERIVED_FROM) in incoming
    assert (outer_start.id, TraceEdgeType.INFORMED) in incoming


# ── Errors ───────────────────────────────────────────────────────────────────────


def test_exception_records_error_and_propagates():
    store = InMemoryTraceStore()

    @traced
    def boom():
        raise ValueError("kaboom")

    with traced_run(store, context_id="c") as run:
        with pytest.raises(ValueError, match="kaboom"):
            boom()

    by_type = _events_by_type(_get_run(store, run))
    assert "boom.start" in by_type
    assert "boom.end" not in by_type  # never completed
    err = by_type["boom.error"]
    assert err.payload.priority is TracePriority.CRITICAL
    assert err.payload.attributes["error_type"] == "ValueError"
    assert err.payload.attributes["message"] == "kaboom"
    # The error event is DERIVED_FROM its start (queryable lineage, like the adapters).
    incoming = {(e.source_id, e.type) for e in store.lineage_edges(err.id)}
    assert (by_type["boom.start"].id, TraceEdgeType.DERIVED_FROM) in incoming


# ── Behavior preservation: capture must never crash the wrapped call ─────────────


class _Unreprable:
    def __repr__(self):
        raise RuntimeError("repr blew up")


def test_unreprable_argument_does_not_break_the_call():
    store = InMemoryTraceStore()

    @traced
    def use(obj):
        return "ok"

    with traced_run(store, context_id="c") as run:
        # An argument whose repr() raises must NOT prevent the call or crash it.
        assert use(_Unreprable()) == "ok"

    by_type = _events_by_type(_get_run(store, run))
    assert by_type["use.end"].payload.attributes["result"] == "'ok'"
    # The arg is recorded as a safe placeholder, not propagated.
    assert "unreprable" in by_type["use.start"].payload.attributes["args"][0]


def test_unreprable_result_does_not_break_the_call():
    store = InMemoryTraceStore()

    @traced
    def make():
        return _Unreprable()

    with traced_run(store, context_id="c") as run:
        result = make()  # must return the value, not raise
        assert isinstance(result, _Unreprable)

    by_type = _events_by_type(_get_run(store, run))
    assert "unreprable" in by_type["make.end"].payload.attributes["result"]


# ── Generators ───────────────────────────────────────────────────────────────────


def test_generator_brackets_full_iteration():
    store = InMemoryTraceStore()

    @traced
    def stream(n):
        for i in range(n):
            yield i

    with traced_run(store, context_id="c") as run:
        # start/end bracket iteration, not object creation: nothing recorded until consumed.
        gen = stream(3)
        assert _get_run(store, run) is None or _get_run(store, run).events == []
        assert list(gen) == [0, 1, 2]

    types = [e.payload.type_identifier for e in _get_run(store, run).events]
    assert types == ["stream.start", "stream.end"]


def test_generator_records_error_raised_during_iteration():
    store = InMemoryTraceStore()

    @traced
    def stream():
        yield 1
        raise ValueError("mid-stream")

    with traced_run(store, context_id="c") as run:
        with pytest.raises(ValueError, match="mid-stream"):
            list(stream())

    by_type = _events_by_type(_get_run(store, run))
    assert "stream.start" in by_type
    assert "stream.end" not in by_type
    assert by_type["stream.error"].payload.attributes["error_type"] == "ValueError"


def test_async_generator_is_traced():
    store = InMemoryTraceStore()

    @traced
    async def astream(n):
        for i in range(n):
            yield i

    async def main():
        with traced_run(store, context_id="c") as run:
            out = [x async for x in astream(2)]
            return run, out

    run, out = asyncio.run(main())
    assert out == [0, 1]
    types = [e.payload.type_identifier for e in _get_run(store, run).events]
    assert types == ["astream.start", "astream.end"]


# ── Threads ──────────────────────────────────────────────────────────────────────


def test_asyncio_to_thread_propagates_the_run():
    store = InMemoryTraceStore()

    @traced
    def blocking(x):
        return x * 2

    async def main():
        with traced_run(store, context_id="c") as run:
            result = await asyncio.to_thread(blocking, 5)
            return run, result

    run, result = asyncio.run(main())
    assert result == 10
    # to_thread copies the context, so the step IS recorded across the thread boundary.
    types = [e.payload.type_identifier for e in _get_run(store, run).events]
    assert types == ["blocking.start", "blocking.end"]


# ── record_event ─────────────────────────────────────────────────────────────────


def test_record_event_inside_run_and_under_step():
    store = InMemoryTraceStore()

    @traced
    def step():
        record_event("decision.made", {"choice": "A"}, priority=TracePriority.CRITICAL)
        return "ok"

    with traced_run(store, context_id="c") as run:
        record_event("plan.chosen", {"strategy": "rag"})
        step()

    rec = _get_run(store, run)
    by_type = _events_by_type(rec)
    # Top-level event has no span; the in-step event shares the step's span.
    assert by_type["plan.chosen"].span_id is None
    assert by_type["decision.made"].span_id == by_type["step.start"].span_id
    assert by_type["decision.made"].payload.priority is TracePriority.CRITICAL


# ── Transparency outside a run ───────────────────────────────────────────────────


def test_decorator_is_transparent_outside_a_run():
    calls = []

    @traced
    def f(x):
        calls.append(x)
        return x * 2

    # No traced_run active: behaves exactly like the undecorated function, records nothing.
    assert f(21) == 42
    assert calls == [21]

    # And then works normally inside a run.
    store = InMemoryTraceStore()
    with traced_run(store, context_id="c") as run:
        f(1)
    assert [e.payload.type_identifier for e in _get_run(store, run).events] == [
        "f.start",
        "f.end",
    ]


def test_record_event_outside_run_is_noop():
    assert record_event("orphan", {"x": 1}) is None


# ── Async ────────────────────────────────────────────────────────────────────────


def test_async_function_is_traced():
    store = InMemoryTraceStore()

    @traced
    async def fetch(url):
        await asyncio.sleep(0)
        return f"body of {url}"

    async def main():
        with traced_run(store, context_id="c") as run:
            result = await fetch("http://x")
            return run, result

    run, result = asyncio.run(main())
    assert result == "body of http://x"
    by_type = _events_by_type(_get_run(store, run))
    assert "fetch.start" in by_type and "fetch.end" in by_type
    assert by_type["fetch.start"].engine_name == "fetch"
    assert by_type["fetch.end"].payload.attributes["result"] == "'body of http://x'"


# ── Fingerprint: structural identity of the instrumented path ───────────────────


def _fingerprint_after(store: SQLiteTraceStore, run_id: uuid.UUID) -> str:
    store.flush()
    rows = store._db.query(
        "SELECT fingerprint FROM runs WHERE run_id = ?", (str(run_id),)
    )
    return rows[0][0]


def test_same_path_shares_fingerprint_different_path_differs():
    @traced
    def a():
        return 1

    @traced
    def b():
        return 2

    with tempfile.TemporaryDirectory() as tmp:
        store = SQLiteTraceStore(TracedEvent, f"{tmp}/t.sqlite", start_writer=False)
        with traced_run(store, context_id="r1") as r1:
            a()
            b()
        with traced_run(store, context_id="r2") as r2:
            a()
            b()
        with traced_run(store, context_id="r3") as r3:
            b()
            a()  # reordered

        store.flush()
        fp1 = _fingerprint_after(store, r1.run_id)
        fp2 = _fingerprint_after(store, r2.run_id)
        fp3 = _fingerprint_after(store, r3.run_id)

    assert fp1 == fp2
    assert fp1 != fp3

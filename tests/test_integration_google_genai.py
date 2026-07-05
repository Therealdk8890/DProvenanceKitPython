"""Tests for the Google GenAI integration.

The adapter wraps a ``google.genai`` client so that ``models.generate_content``
calls are traced. We drive it with a fake client — no ``google-genai`` install and
no network — so the full translation (start/end events, error handling, lineage
edge, span pairing, response capture) is verified in default CI.
"""

from __future__ import annotations

import pytest

from dprovenancekit import InMemoryTraceStore
from dprovenancekit.edge import TraceEdgeType
from dprovenancekit.integrations.google_genai import (
    DProvenanceGenAIWrapper,
    GoogleGenAITraceEvent,
)
from dprovenancekit.kit import DProvenanceKit

# ── Minimal stand-ins for the google.genai client ───────────────────────────────


class _FakeResponse:
    def __init__(self, text: str):
        self.text = text


class _FakeModels:
    """Mimics client.models: records the args it was called with and replies."""

    def __init__(self, reply: str = "generated text", raises: Exception = None):
        self.reply = reply
        self.raises = raises
        self.calls: list = []

    def generate_content(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.raises is not None:
            raise self.raises
        return _FakeResponse(self.reply)


class _FakeClient:
    def __init__(self, models: _FakeModels):
        self.models = models


# ── Helpers ─────────────────────────────────────────────────────────────────────


def _wrap(store, models, **kw):
    """Open a run and return (wrapper, run) so tests can read events afterwards."""
    kit = DProvenanceKit(GoogleGenAITraceEvent)
    run_cm = kit.run(context_id="genai", store=store)
    run = run_cm.__enter__()
    wrapper = DProvenanceGenAIWrapper(_FakeClient(models), run, **kw)
    return wrapper, run, run_cm


def _types(store, run):
    got = store.get_run(run.run_id)
    return [e.payload.type_identifier for e in got.events]


# ── Success path ────────────────────────────────────────────────────────────────


def test_generate_content_records_start_and_end_and_passes_response_through():
    store = InMemoryTraceStore()
    models = _FakeModels(reply="hello from gemini")
    wrapper, run, run_cm = _wrap(store, models)
    try:
        resp = wrapper.models.generate_content(
            model="gemini-2.0-flash", contents="hi"
        )
    finally:
        run_cm.__exit__(None, None, None)

    # The real client was called with the caller's arguments untouched.
    assert models.calls == [((), {"model": "gemini-2.0-flash", "contents": "hi"})]
    # The wrapper returns the underlying response unchanged.
    assert resp.text == "hello from gemini"

    got = store.get_run(run.run_id)
    started, ended = got.events
    assert [e.payload.type_identifier for e in got.events] == [
        "generateContentStarted",
        "generateContentEnded",
    ]
    assert started.engine_name == "google_genai"
    assert started.payload.attributes["model"] == "gemini-2.0-flash"
    assert ended.payload.attributes["response_preview"] == "hello from gemini"
    # Start and end of one call share a single span.
    assert started.span_id == ended.span_id
    assert started.span_id is not None

    # The end event is DERIVED_FROM the start event (by event id, not span uuid).
    incoming = {(e.source_id, e.type) for e in store.lineage_edges(ended.id)}
    assert (started.id, TraceEdgeType.DERIVED_FROM) in incoming


def test_generate_content_accepts_model_as_positional_arg():
    store = InMemoryTraceStore()
    wrapper, run, run_cm = _wrap(store, _FakeModels())
    try:
        wrapper.models.generate_content("gemini-1.5-pro", contents="yo")
    finally:
        run_cm.__exit__(None, None, None)

    started = store.get_run(run.run_id).events[0]
    assert started.payload.attributes["model"] == "gemini-1.5-pro"


# ── Error path ──────────────────────────────────────────────────────────────────


def test_generate_content_records_error_and_reraises():
    store = InMemoryTraceStore()
    boom = RuntimeError("quota exceeded")
    wrapper, run, run_cm = _wrap(store, _FakeModels(raises=boom))
    try:
        with pytest.raises(RuntimeError, match="quota exceeded") as excinfo:
            wrapper.models.generate_content(model="gemini-2.0-flash", contents="hi")
        # The original exception is re-raised (not wrapped/replaced).
        assert excinfo.value is boom
    finally:
        run_cm.__exit__(None, None, None)

    got = store.get_run(run.run_id)
    started, errored = got.events
    assert [e.payload.type_identifier for e in got.events] == [
        "generateContentStarted",
        "generateContentError",
    ]
    assert errored.payload.attributes["error"] == "quota exceeded"
    incoming = {(e.source_id, e.type) for e in store.lineage_edges(errored.id)}
    assert (started.id, TraceEdgeType.DERIVED_FROM) in incoming


# ── Options ─────────────────────────────────────────────────────────────────────


def test_link_lifecycle_off_produces_no_edges():
    store = InMemoryTraceStore()
    wrapper, run, run_cm = _wrap(store, _FakeModels(), link_lifecycle=False)
    try:
        wrapper.models.generate_content(model="gemini-2.0-flash", contents="hi")
    finally:
        run_cm.__exit__(None, None, None)

    ended = store.get_run(run.run_id).events[1]
    assert store.lineage_edges(ended.id) == []

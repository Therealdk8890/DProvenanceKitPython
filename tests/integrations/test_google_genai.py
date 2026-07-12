"""Tests for the Google GenAI integration.

The adapter wraps a ``google.genai`` client so that ``models.generate_content`` calls
are traced. We drive it with fake client objects — no ``google-genai`` install and no
network — so the full translation is verified in default CI:

* start/end/error events, span pairing, lineage edges, engine = the request's model;
* **pass-through**: every attribute the wrapper does not trace delegates to the wrapped
  object, so the wrapper is a drop-in for the real client;
* **defensive response extraction**: ``response.text`` is a property that can return
  ``None`` or raise (e.g. safety-blocked) — tracing must record a placeholder and
  return the response, never raise into user code.
"""

from __future__ import annotations

import pytest

from dprovenancekit import InMemoryTraceStore, TracePriority
from dprovenancekit.edge import TraceEdgeType
from dprovenancekit.integrations.google_genai import (
    DProvenanceGenAIWrapper,
    GoogleGenAITraceEvent,
)
from dprovenancekit.kit import DProvenanceKit

# ── Minimal stand-ins for the google.genai client ───────────────────────────────


class _FakeResponse:
    """Mimics ``GenerateContentResponse``: ``text`` is a *property* (as in the real
    SDK) so tests can make it return ``None`` or raise, and ``usage_metadata`` is
    optional."""

    def __init__(self, text=None, text_raises=None, usage_metadata=None):
        self._text = text
        self._text_raises = text_raises
        self.usage_metadata = usage_metadata

    @property
    def text(self):
        if self._text_raises is not None:
            raise self._text_raises
        return self._text


class _FakeUsageMetadata:
    def __init__(self, prompt=None, candidates=None, total=None):
        self.prompt_token_count = prompt
        self.candidates_token_count = candidates
        self.total_token_count = total


class _FakeModels:
    """Mimics ``client.models``: records the args it was called with and replies.

    Also carries untraced surfaces (``generate_content_stream``, ``embed_content``)
    so tests can prove the wrapper delegates everything it does not wrap.
    """

    def __init__(self, response=None, raises: Exception = None):
        self.response = (
            response if response is not None else _FakeResponse(text="generated text")
        )
        self.raises = raises
        self.calls: list = []

    def generate_content(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.raises is not None:
            raise self.raises
        return self.response

    def generate_content_stream(self, *args, **kwargs):
        return iter(["chunk-1", "chunk-2"])

    def embed_content(self, *args, **kwargs):
        return "embedding"


class _FakeChats:
    pass


class _FakeAio:
    pass


class _FakeClient:
    def __init__(self, models: _FakeModels):
        self.models = models
        self.chats = _FakeChats()
        self.aio = _FakeAio()


# ── Helpers ─────────────────────────────────────────────────────────────────────


def _wrap(store, models, **kw):
    """Open a run and return (wrapper, run, run_cm) so tests can read events after."""
    kit = DProvenanceKit(GoogleGenAITraceEvent)
    run_cm = kit.run(context_id="genai", store=store)
    run = run_cm.__enter__()
    wrapper = DProvenanceGenAIWrapper(_FakeClient(models), run, **kw)
    return wrapper, run, run_cm


def _run_one_call(response=None, **wrap_kw):
    """Trace a single successful generate_content call; return (recorded run, resp)."""
    store = InMemoryTraceStore()
    models = _FakeModels(response=response)
    wrapper, run, run_cm = _wrap(store, models, **wrap_kw)
    try:
        resp = wrapper.models.generate_content(model="gemini-2.0-flash", contents="hi")
    finally:
        run_cm.__exit__(None, None, None)
    return store.get_run(run.run_id), resp


# ── Event type ──────────────────────────────────────────────────────────────────


def test_event_roundtrip_and_canonical_encoding():
    ev = GoogleGenAITraceEvent.make(
        "generateContentEnded",
        TracePriority.STRUCTURAL,
        {"model": "gemini-2.0-flash", "response_preview": "r"},
    )
    assert ev.type_identifier == "generateContentEnded"
    assert ev.priority is TracePriority.STRUCTURAL
    assert ev.encode().decode() == (
        '{"model": "gemini-2.0-flash", "priority": 2, "response_preview": "r", '
        '"type": "generateContentEnded"}'
    )
    assert GoogleGenAITraceEvent.decode(ev.encode()) == ev


# ── Success path ────────────────────────────────────────────────────────────────


def test_generate_content_records_start_and_end_and_passes_response_through():
    store = InMemoryTraceStore()
    models = _FakeModels(response=_FakeResponse(text="hello from gemini"))
    wrapper, run, run_cm = _wrap(store, models)
    try:
        resp = wrapper.models.generate_content(model="gemini-2.0-flash", contents="hi")
    finally:
        run_cm.__exit__(None, None, None)

    # The real client was called with the caller's arguments untouched.
    assert models.calls == [((), {"model": "gemini-2.0-flash", "contents": "hi"})]
    # The wrapper returns the underlying response unchanged.
    assert resp is models.response

    got = store.get_run(run.run_id)
    started, ended = got.events
    assert [e.payload.type_identifier for e in got.events] == [
        "generateContentStarted",
        "generateContentEnded",
    ]
    # Engine = the model identity (matching the other adapters and otel_ingest),
    # not a framework literal.
    assert started.engine_name == "gemini-2.0-flash"
    assert ended.engine_name == "gemini-2.0-flash"
    assert started.payload.attributes["model"] == "gemini-2.0-flash"
    assert ended.payload.attributes["model"] == "gemini-2.0-flash"
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
    assert started.engine_name == "gemini-1.5-pro"


def test_generate_content_without_model_records_unknown():
    store = InMemoryTraceStore()
    wrapper, run, run_cm = _wrap(store, _FakeModels())
    try:
        wrapper.models.generate_content(contents="yo")
    finally:
        run_cm.__exit__(None, None, None)

    started = store.get_run(run.run_id).events[0]
    assert started.payload.attributes["model"] == "unknown"
    assert started.engine_name == "unknown"


def test_long_response_text_is_truncated():
    run, _ = _run_one_call(response=_FakeResponse(text="x" * 1000))
    preview = run.events[1].payload.attributes["response_preview"]
    assert preview == "x" * 500 + "…"


# ── Defensive response extraction ───────────────────────────────────────────────


def test_none_text_records_end_without_preview_and_without_type_error():
    """``response.text`` is Optional in the SDK (e.g. a pure function-call response);
    ``None`` must not blow up slicing — the preview is simply omitted."""
    response = _FakeResponse(text=None)
    run, resp = _run_one_call(response=response)
    assert resp is response
    ended = run.events[1]
    assert ended.payload.type_identifier == "generateContentEnded"
    assert "response_preview" not in ended.payload.attributes


def test_raising_text_property_records_placeholder_and_still_returns_response():
    """``response.text`` raises on safety-blocked responses. Tracing sits between the
    API call and the return, so it must swallow the raise — a placeholder is recorded
    and the user still gets their response object back."""
    response = _FakeResponse(text_raises=ValueError("no candidates: blocked"))
    run, resp = _run_one_call(response=response)
    assert resp is response
    ended = run.events[1]
    assert ended.payload.type_identifier == "generateContentEnded"
    assert ended.payload.attributes["response_preview"] == "<text unavailable>"


def test_response_without_text_attribute_records_placeholder():
    class _Bare:
        usage_metadata = None

    run, resp = _run_one_call(response=_Bare())
    assert isinstance(resp, _Bare)
    ended = run.events[1]
    assert ended.payload.attributes["response_preview"] == "<text unavailable>"


# ── Token usage ─────────────────────────────────────────────────────────────────


def test_usage_metadata_tokens_are_captured():
    response = _FakeResponse(
        text="ok", usage_metadata=_FakeUsageMetadata(prompt=11, candidates=7, total=18)
    )
    run, _ = _run_one_call(response=response)
    attrs = run.events[1].payload.attributes
    assert attrs["prompt_token_count"] == 11
    assert attrs["candidates_token_count"] == 7
    assert attrs["total_token_count"] == 18


def test_usage_metadata_as_mapping_is_captured():
    response = _FakeResponse(
        text="ok", usage_metadata={"prompt_token_count": 3, "total_token_count": 5}
    )
    run, _ = _run_one_call(response=response)
    attrs = run.events[1].payload.attributes
    assert attrs["prompt_token_count"] == 3
    assert attrs["total_token_count"] == 5
    assert "candidates_token_count" not in attrs


def test_missing_or_raising_usage_metadata_is_omitted():
    class _RaisingUsageResponse:
        text = "ok"

        @property
        def usage_metadata(self):
            raise RuntimeError("lazily computed and broken")

    for response in (_FakeResponse(text="ok"), _RaisingUsageResponse()):
        run, _ = _run_one_call(response=response)
        attrs = run.events[1].payload.attributes
        assert "prompt_token_count" not in attrs
        assert "candidates_token_count" not in attrs
        assert "total_token_count" not in attrs


# ── Error path ──────────────────────────────────────────────────────────────────


def test_generate_content_records_error_as_critical_and_reraises():
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
    # Errors are never droppable telemetry.
    assert errored.payload.priority is TracePriority.CRITICAL
    assert errored.payload.attributes["error"] == "quota exceeded"
    assert errored.payload.attributes["model"] == "gemini-2.0-flash"
    assert errored.engine_name == "gemini-2.0-flash"
    assert started.span_id == errored.span_id
    incoming = {(e.source_id, e.type) for e in store.lineage_edges(errored.id)}
    assert (started.id, TraceEdgeType.DERIVED_FROM) in incoming


# ── Pass-through (the wrapper is a drop-in for the real client) ──────────────────


def test_unwrapped_models_attributes_delegate_to_the_real_models_client():
    store = InMemoryTraceStore()
    models = _FakeModels()
    wrapper, run, run_cm = _wrap(store, models)
    try:
        # Untraced SDK surfaces resolve to the real client.models and work unchanged.
        assert list(wrapper.models.generate_content_stream(model="m")) == [
            "chunk-1",
            "chunk-2",
        ]
        assert wrapper.models.embed_content(contents="hi") == "embedding"
        assert wrapper.models.calls == []  # delegated reads, not traced calls
    finally:
        run_cm.__exit__(None, None, None)

    # Nothing was recorded for pass-through access (an event-less run may not even
    # materialize in the store).
    got = store.get_run(run.run_id)
    assert got is None or got.events == []


def test_client_level_attributes_delegate_to_the_real_client():
    store = InMemoryTraceStore()
    models = _FakeModels()
    wrapper, run, run_cm = _wrap(store, models)
    try:
        # chats / aio (and any future surface) resolve to the wrapped client...
        assert wrapper.chats is wrapper.client.chats
        assert wrapper.aio is wrapper.client.aio
        # ...while .models stays the tracing wrapper, not the raw client.models.
        assert wrapper.models is not models
    finally:
        run_cm.__exit__(None, None, None)


def test_missing_attributes_still_raise_attribute_error():
    store = InMemoryTraceStore()
    wrapper, run, run_cm = _wrap(store, _FakeModels())
    try:
        with pytest.raises(AttributeError):
            wrapper.no_such_surface
        with pytest.raises(AttributeError):
            wrapper.models.no_such_method
    finally:
        run_cm.__exit__(None, None, None)


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

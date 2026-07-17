"""Tests for the google-genai integration wrapper (``dprovenancekit.integrations.google_genai``).

The wrapper is driven with a fake client so the tests run without the google-genai SDK.
"""

from __future__ import annotations

import sys

from dprovenancekit import DProvenanceKit, InMemoryTraceStore
from dprovenancekit.integrations.google_genai import DProvenanceGenAIWrapper

sys.path.insert(0, "tests")
from conftest import TestEvent  # noqa: E402


class _FakeResponse:
    text = "hello"
    candidates: list = []
    usage_metadata = None


class _FakeModels:
    def generate_content(self, *args, **kwargs):
        return _FakeResponse()


class _FakeClient:
    def __init__(self):
        self.models = _FakeModels()


def test_generate_content_attaches_to_enclosing_span_not_grandparent():
    """A generate_content call nested inside two spans must record its events as children
    of the *enclosing* span. The wrapper originally set only current_span_id, leaving
    parent_span_id pointing at the enclosing span's parent — the grandparent — so nested
    LLM calls attached to the wrong node in the span tree."""
    store = InMemoryTraceStore()
    kit = DProvenanceKit(TestEvent)
    with kit.run(context_id="ctx", store=store) as run:
        with kit.with_span("outer"):
            with kit.with_span("inner"):
                wrapped = DProvenanceGenAIWrapper(_FakeClient(), run)
                wrapped.models.generate_content(model="gemini-1.5", contents="x")
        run_id = run.run_id

    calls = [
        e
        for e in store.get_run(run_id).events
        if e.payload.type_identifier.startswith("generateContent")
    ]
    assert calls, "expected generateContent events to be recorded"
    for e in calls:
        assert e.parent_span_id == "inner"  # the enclosing span, not "outer"
        assert e.span_id not in (None, "inner", "outer")  # its own fresh span
    # start and end share one span so the pair reads as a single node.
    assert len({e.span_id for e in calls}) == 1


def test_generate_content_without_enclosing_span_is_a_root_call():
    store = InMemoryTraceStore()
    kit = DProvenanceKit(TestEvent)
    with kit.run(context_id="ctx", store=store) as run:
        wrapped = DProvenanceGenAIWrapper(_FakeClient(), run)
        wrapped.models.generate_content(model="gemini-1.5", contents="x")
        run_id = run.run_id

    calls = [
        e
        for e in store.get_run(run_id).events
        if e.payload.type_identifier.startswith("generateContent")
    ]
    assert calls
    for e in calls:
        assert e.parent_span_id is None

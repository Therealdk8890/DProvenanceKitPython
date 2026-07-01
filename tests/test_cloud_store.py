"""Ports CloudTraceStoreTests using an injected fake transport."""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass
from urllib.parse import urlparse

import pytest

from dprovenancekit import CloudTraceStore, NotImplementedTraceError, TraceEvent, TracePriority, TraceableEvent


@dataclass(frozen=True)
class CloudEvent(TraceableEvent):
    @property
    def type_identifier(self) -> str:
        return "somethingHappened"

    @property
    def priority(self) -> TracePriority:
        return TracePriority.TELEMETRY


def _event(seq=1):
    return TraceEvent(
        run_id=uuid.uuid4(), context_id="ctx1", engine_name="test", schema_version=1,
        sequence=seq, span_id=None, parent_span_id=None, payload=CloudEvent(),
    )


def test_successful_ingest():
    seen = {}

    def transport(method, url, headers, body):
        seen["path"] = urlparse(url).path
        seen["auth"] = headers.get("Authorization")
        return 200, b""

    store = CloudTraceStore(CloudEvent, "https://api.dprovenance.cloud", "test-key", transport=transport, start_writer=False)
    store.record(_event())
    store.flush()

    assert seen["path"] == "/ingest"
    assert seen["auth"] == "Bearer test-key"


def test_query_dsl_serialization_and_not_implemented():
    from dprovenancekit import TraceQueryDSL

    def transport(method, url, headers, body):
        assert urlparse(url).path == "/query"
        return 501, b""

    store = CloudTraceStore(CloudEvent, "https://api.dprovenance.cloud", "test-key", transport=transport, start_writer=False)
    dsl = TraceQueryDSL().requiring_step("somethingHappened")
    with pytest.raises(NotImplementedTraceError):
        store.query_runs(dsl)


def test_retry_and_backoff():
    attempts = {"n": 0}
    lock = threading.Lock()

    def transport(method, url, headers, body):
        with lock:
            attempts["n"] += 1
            n = attempts["n"]
        if n < 3:
            return 500, b""
        return 200, b""

    store = CloudTraceStore(CloudEvent, "https://api.dprovenance.cloud", "test-key", transport=transport, start_writer=False)
    store.record(_event())
    store.flush()
    assert attempts["n"] == 3

# git-blob-rewrite

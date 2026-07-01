"""Ports CloudTraceStoreChaosTests using an injected fake transport."""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass
from urllib.parse import urlparse

import pytest

from dprovenancekit import (
    BufferCapacity,
    CloudTraceStore,
    CloudWriter,
    FlushTimedOut,
    OfflineConfig,
    TracePriority,
    TraceWriteBuffer,
    TraceEvent,
    TraceableEvent,
)
from dprovenancekit.event import TraceEventRow


@dataclass(frozen=True)
class ChaosEvent(TraceableEvent):
    kind: str  # tiny | huge
    blob: str = ""

    @property
    def type_identifier(self) -> str:
        return "chaos"

    @property
    def priority(self) -> TracePriority:
        return TracePriority.STRUCTURAL

    def to_dict(self):
        return {"kind": self.kind, "blob": self.blob}

    @classmethod
    def from_dict(cls, data):
        return cls(data["kind"], data.get("blob", ""))

    @staticmethod
    def tiny():
        return ChaosEvent("tiny")

    @staticmethod
    def huge(size):
        return ChaosEvent("huge", "0" * size)


def _event(payload, seq):
    return TraceEvent(
        run_id=uuid.uuid4(),
        context_id="1",
        engine_name="test",
        schema_version=1,
        sequence=seq,
        span_id=None,
        parent_span_id=None,
        payload=payload,
    )


def test_write_amplification_defense():
    config = OfflineConfig(
        capacity=BufferCapacity(
            max_items=1000, max_bytes=1_000_000, max_event_size_bytes=500_000
        ),
    )

    def transport(method, url, headers, body):
        return 200, b""

    store = CloudTraceStore(
        ChaosEvent,
        "https://api.dprovenance.cloud",
        "test",
        config=config,
        transport=transport,
        start_writer=False,
    )
    store.record(_event(ChaosEvent.tiny(), 1))
    store.record(_event(ChaosEvent.huge(600_000), 2))  # ~600KB encoded

    assert store.drop_stats.structural == 1
    store.flush()


def test_poison_batch_quarantine():
    attempts = {"n": 0}

    def transport(method, url, headers, body):
        if urlparse(url).path == "/capabilities":
            return 200, b""
        attempts["n"] += 1
        return (400, b"") if attempts["n"] == 1 else (200, b"")

    store = CloudTraceStore(
        ChaosEvent,
        "https://api.dprovenance.cloud",
        "test",
        transport=transport,
        start_writer=False,
    )
    store.record(_event(ChaosEvent.tiny(), 1))
    store.flush()
    assert attempts["n"] == 1  # 400 → quarantined

    store.record(_event(ChaosEvent.tiny(), 2))
    store.flush()
    assert attempts["n"] == 2  # advanced and succeeded


def test_concurrent_enqueue_and_flush():
    total = {"n": 0}
    lock = threading.Lock()

    def transport(method, url, headers, body):
        if urlparse(url).path == "/capabilities":
            return 200, b""
        if body:
            try:
                items = json.loads(body.decode("utf-8"))
                with lock:
                    total["n"] += len(items)
            except Exception:
                pass
        return 200, b""

    store = CloudTraceStore(
        ChaosEvent,
        "https://api.dprovenance.cloud",
        "test",
        transport=transport,
        start_writer=False,
    )

    def worker():
        for i in range(100):
            store.record(_event(ChaosEvent.tiny(), i))

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    store.flush()
    assert total["n"] == 1000


def test_flush_times_out_on_sustained_outage_instead_of_hanging():
    buffer = TraceWriteBuffer(config=OfflineConfig())
    buffer.enqueue(
        TraceEventRow(
            id=str(uuid.uuid4()),
            run_id=str(uuid.uuid4()),
            context_id="1",
            priority=int(TracePriority.STRUCTURAL),
            sequence=1,
            engine="test",
            span_id=None,
            parent_span_id=None,
            type="chaos",
            payload=b"x",
            timestamp=0,
        )
    )
    assert buffer.current_depth == 1

    def transport(method, url, headers, body):
        raise ConnectionError("cannot connect to host")

    writer = CloudWriter(
        "https://api.dprovenance.cloud/ingest", "test", buffer, transport=transport
    )

    start = time.time()
    with pytest.raises(FlushTimedOut):
        writer.flush(timeout=1.0)
    elapsed = time.time() - start
    assert elapsed < 8.0

"""Cloud trace store: buffered, retrying, circuit-broken delivery to an HTTP endpoint.

Networking is abstracted behind a ``transport`` callable
``(method, url, headers, body) -> (status_code, data)`` (raising on connection failure),
so the store can be driven by a fake transport in tests exactly as the Swift version
injects a ``URLSession``. The default transport uses :mod:`urllib`.
"""

from __future__ import annotations

import json
import random
import threading
import time
import urllib.error
import urllib.request
import uuid
from typing import Callable, Dict, List, Optional, Tuple, Type

from .circuit_breaker import CircuitBreaker
from .config import OfflineConfig
from .drop_stats import TraceDropStats
from .edge import TraceEdge, TraceEdgeType
from .event import TraceableEvent, TraceEvent, TraceEventRow
from .query import TraceQueryDSL, TraceRun
from .store import NotImplementedTraceError, TraceStore
from .write_buffer import TraceWriteBuffer

Transport = Callable[[str, str, Dict[str, str], Optional[bytes]], Tuple[int, bytes]]


def default_transport(method: str, url: str, headers: Dict[str, str], body: Optional[bytes]) -> Tuple[int, bytes]:
    request = urllib.request.Request(url=url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read() if err.fp else b""


class CloudTraceStoreError(Exception):
    pass


class UnsupportedSchemaError(CloudTraceStoreError):
    def __init__(self, expected: str, received: str):
        super().__init__(f"unsupported schema: expected {expected}, received {received}")
        self.expected = expected
        self.received = received


class ServerError(CloudTraceStoreError):
    def __init__(self, status: int):
        super().__init__(f"server error: {status}")
        self.status = status


class CloudWriterError(Exception):
    pass


class FlushTimedOut(CloudWriterError):
    def __init__(self, undelivered: int):
        super().__init__(f"flush timed out, {undelivered} undelivered")
        self.undelivered = undelivered


class CloudWriter:
    def __init__(self, endpoint: str, api_key: str, buffer: TraceWriteBuffer, transport: Transport = default_transport):
        self._endpoint = endpoint
        self._api_key = api_key
        self._buffer = buffer
        self._transport = transport

        self._thread: Optional[threading.Thread] = None
        self._shutting_down = threading.Event()
        self._state_lock = threading.Lock()
        self._sending = False
        self._inflight_batch: Optional[List[TraceEventRow]] = None
        self._attempt_count = 0
        self._quarantine_queue: List[List[TraceEventRow]] = []
        self._circuit_breaker = CircuitBreaker()

    def start(self) -> None:
        with self._state_lock:
            if self._thread is not None:
                return
            self._thread = threading.Thread(target=self._loop, name="dprov-cloud-writer", daemon=True)
            self._thread.start()

    def _loop(self) -> None:
        while not self._shutting_down.is_set():
            self._process_next_batch(max_batch=1000)
            time.sleep(0.5)

    def flush(self, timeout: float = 30.0) -> None:
        deadline = time.time() + timeout
        while self._buffer.current_depth > 0 or self._inflight_batch is not None:
            if time.time() >= deadline:
                undelivered = self._buffer.current_depth + (
                    len(self._inflight_batch) if self._inflight_batch else 0
                )
                raise FlushTimedOut(undelivered)
            if self._sending:
                time.sleep(0.02)
                continue
            self._process_next_batch(drain_all=True, deadline=deadline)
            time.sleep(0.02)

    def shutdown(self) -> None:
        self._shutting_down.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
        try:
            self.flush()
        except CloudWriterError:
            pass

    def get_quarantined_events(self) -> List[TraceEventRow]:
        with self._state_lock:
            return [row for batch in self._quarantine_queue for row in batch]

    def _process_next_batch(self, drain_all: bool = False, max_batch: int = 1000, deadline: Optional[float] = None) -> None:
        with self._state_lock:
            if self._sending:
                return
            self._sending = True
        try:
            wait_time = self._circuit_breaker.time_until_allowed()
            if wait_time > 0:
                if deadline is not None and time.time() + wait_time >= deadline:
                    return
                time.sleep(wait_time)

            if not self._circuit_breaker.allow_request():
                return

            if self._inflight_batch is not None:
                batch = self._inflight_batch
            else:
                drained = self._buffer.flush_all() if drain_all else self._buffer.drain(max_batch)
                if not drained:
                    return
                batch = drained
                self._inflight_batch = batch
                self._attempt_count = 0

            max_attempts = 10
            base_backoff = 1.0
            max_backoff = 60.0

            while self._attempt_count < max_attempts:
                try:
                    status_code = self._send_batch(batch)
                    if status_code == 400:
                        print("🚨 [DProvenanceKit] Poison batch detected (400 Bad Request). Quarantining.")
                        with self._state_lock:
                            self._quarantine_queue.append(batch)
                        self._inflight_batch = None
                        self._attempt_count = 0
                        self._circuit_breaker.record_success()
                        return
                    self._inflight_batch = None
                    self._attempt_count = 0
                    self._circuit_breaker.record_success()
                    return
                except Exception:
                    self._attempt_count += 1
                    self._circuit_breaker.record_failure()
                    if self._attempt_count >= max_attempts:
                        print(f"🚨 [DProvenanceKit] Batch failed {max_attempts} times. Quarantining.")
                        with self._state_lock:
                            self._quarantine_queue.append(batch)
                        self._inflight_batch = None
                        self._attempt_count = 0
                        return
                    if not self._circuit_breaker.allow_request():
                        return
                    if self._attempt_count == 1:
                        backoff = random.uniform(0.1, 1.0)
                    else:
                        cap = min(max_backoff, base_backoff * (2.0 ** self._attempt_count))
                        backoff = random.uniform(0.0, cap)
                    if deadline is not None and time.time() + backoff >= deadline:
                        return
                    time.sleep(backoff)
        finally:
            with self._state_lock:
                self._sending = False

    def _send_batch(self, events: List[TraceEventRow]) -> int:
        payload = []
        for e in events:
            try:
                decoded_payload = json.loads(e.payload.decode("utf-8"))
            except Exception:
                import base64

                decoded_payload = base64.b64encode(e.payload).decode("ascii")
            payload.append(
                {
                    "id": e.id,
                    "run_id": e.run_id,
                    "context_id": e.context_id,
                    "priority": e.priority,
                    "sequence": e.sequence,
                    "engine": e.engine,
                    "span_id": e.span_id,
                    "parent_span_id": e.parent_span_id,
                    "type": e.type,
                    "payload": decoded_payload,
                    "timestamp": e.timestamp,
                }
            )
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        status, _ = self._transport("POST", self._endpoint, headers, body)
        if not (200 <= status <= 299) and status != 400:
            raise ServerError(status)
        return status


class CloudTraceStore(TraceStore):
    def __init__(
        self,
        event_type: Type[TraceableEvent],
        endpoint: str,
        api_key: str,
        config: Optional[OfflineConfig] = None,
        transport: Transport = default_transport,
        start_writer: bool = True,
    ):
        self._event_type = event_type
        self._endpoint = endpoint.rstrip("/")
        self._api_key = api_key
        self._buffer = TraceWriteBuffer(config=config if config is not None else OfflineConfig())
        self._transport = transport
        self._writer = CloudWriter(
            endpoint=self._endpoint + "/ingest", api_key=api_key, buffer=self._buffer, transport=transport
        )
        if start_writer:
            self._writer.start()

    def record(self, event: TraceEvent) -> None:
        try:
            payload_data = event.payload.encode()
        except Exception:
            return
        row = TraceEventRow(
            id=str(uuid.uuid4()),
            run_id=str(event.run_id),
            context_id=event.context_id,
            priority=int(event.payload.priority),
            sequence=int(event.sequence),
            engine=event.engine_name,
            span_id=event.span_id,
            parent_span_id=event.parent_span_id,
            type=event.payload.type_identifier,
            payload=payload_data,
            timestamp=int(event.timestamp * 1_000_000),
        )
        self._buffer.enqueue(row)

    def link(self, source: uuid.UUID, target: uuid.UUID, type: TraceEdgeType) -> None:
        self._buffer.enqueue_edge(TraceEdge(source_id=source, target_id=target, type=type))

    def flush(self, timeout: Optional[float] = None) -> None:
        if timeout is None:
            self._writer.flush()
        else:
            self._writer.flush(timeout=timeout)

    @property
    def drop_stats(self) -> TraceDropStats:
        return self._buffer.drop_stats

    def negotiate_capabilities(self) -> None:
        headers = {"Authorization": f"Bearer {self._api_key}"}
        status, _ = self._transport("GET", self._endpoint + "/capabilities", headers, None)
        if not (200 <= status <= 299):
            raise ServerError(status)

    def query_runs(self, dsl: TraceQueryDSL) -> List[TraceRun]:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload = {"schemaVersion": dsl.schema_version, "dsl": _serialize_node(dsl.ast), "limit": 100}
        body = json.dumps(payload).encode("utf-8")
        status, data = self._transport("POST", self._endpoint + "/query", headers, body)

        if status in (400, 422):
            try:
                err = json.loads(data.decode("utf-8"))
            except Exception:
                err = {}
            if err.get("error") == "UNSUPPORTED_SCHEMA":
                raise UnsupportedSchemaError(err.get("expected", ""), err.get("received", ""))
        if status == 501:
            raise NotImplementedTraceError("not implemented")
        if not (200 <= status <= 299):
            raise ServerError(status)
        return []

    def query_quarantined_events(self, dsl: TraceQueryDSL) -> List[TraceEvent]:
        rows = self._writer.get_quarantined_events()
        all_events: List[TraceEvent] = []
        for row in rows:
            try:
                run_id = uuid.UUID(row.run_id)
                payload = self._event_type.decode(row.payload)
                event_id = uuid.UUID(row.id)
            except Exception:
                continue
            all_events.append(
                TraceEvent(
                    id=event_id,
                    run_id=run_id,
                    context_id=row.context_id,
                    engine_name=row.engine or "Unknown",
                    schema_version=1,
                    sequence=int(row.sequence),
                    span_id=row.span_id,
                    parent_span_id=row.parent_span_id,
                    payload=payload,
                    timestamp=float(row.timestamp) / 1_000_000.0,
                )
            )

        by_run: Dict[uuid.UUID, List[TraceEvent]] = {}
        for e in all_events:
            by_run.setdefault(e.run_id, []).append(e)

        matched: List[TraceEvent] = []
        for run_id, events in by_run.items():
            ordered = sorted(events, key=lambda e: e.sequence)
            run = TraceRun(run_id=run_id, context_id=ordered[0].context_id, events=ordered)
            if dsl.ast.evaluate(run):
                matched.extend(events)
        return matched

    def lineage_edges(self, id: uuid.UUID) -> List[TraceEdge]:
        raise NotImplementedTraceError("not implemented")

    def impact_edges(self, id: uuid.UUID) -> List[TraceEdge]:
        raise NotImplementedTraceError("not implemented")

    def get_events(self, ids):
        raise NotImplementedTraceError("not implemented")


def _serialize_node(node) -> dict:
    """Serialize a query AST node to a plain dict (the wire form for the cloud query)."""
    from .query import (
        AfterNode,
        AndNode,
        BeforeNode,
        ContainsStep,
        ContextIDEquals,
        EngineNameEquals,
        MissingStep,
        NotNode,
        OrNode,
        SequenceNode,
    )

    if isinstance(node, AndNode):
        return {"type": "and", "nodes": [_serialize_node(n) for n in node.nodes]}
    if isinstance(node, OrNode):
        return {"type": "or", "nodes": [_serialize_node(n) for n in node.nodes]}
    if isinstance(node, NotNode):
        return {"type": "not", "node": _serialize_node(node.node)}
    if isinstance(node, ContextIDEquals):
        return {"type": "contextIDEquals", "id": node.context_id}
    if isinstance(node, EngineNameEquals):
        return {"type": "engineNameEquals", "name": node.name}
    if isinstance(node, ContainsStep):
        return {"type": "containsStep", "step": node.step}
    if isinstance(node, MissingStep):
        return {"type": "missingStep", "step": node.step}
    if isinstance(node, SequenceNode):
        return {"type": "sequence", "steps": list(node.steps)}
    if isinstance(node, AfterNode):
        return {"type": "after", "step": node.step, "followedBy": node.followed_by}
    if isinstance(node, BeforeNode):
        return {"type": "before", "step": node.step, "precededBy": node.preceded_by}
    raise NotImplementedTraceError(
        f"query node {type(node).__name__} is not supported over the cloud query wire "
        "(Trace Spec v1); it is available on the in-memory and SQLite backends"
    )


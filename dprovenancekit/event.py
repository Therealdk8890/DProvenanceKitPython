"""The event model.

An event is anything conforming to :class:`TraceableEvent`, exposing a stable
``type_identifier`` and a ``priority``. Internally an event travels as a generic
envelope :class:`TraceEvent` carrying run/context/engine/sequence/span lineage. At the
storage boundary it is flattened to a type-erased :class:`TraceEventRow` (payload as
JSON bytes).
"""

from __future__ import annotations

import json
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, fields, is_dataclass
from typing import Optional

from .priority import TracePriority


class TraceableEvent(ABC):
    """Protocol for an event payload that can be recorded by DProvenanceKit.

    Consumers subclass this (typically as a frozen dataclass) and implement
    ``type_identifier`` and ``priority``. The ``type_identifier`` is the stable key
    diffing and querying are defined over — it MUST be stable across schema versions.

    Codability is provided by :meth:`encode` / :meth:`decode`, which default to JSON
    over the dataclass fields. Override :meth:`to_dict` / :meth:`from_dict` (or
    :meth:`encode` / :meth:`decode` directly) for custom serialization — e.g. to drop a
    field, or to deliberately fail encoding.
    """

    @property
    @abstractmethod
    def type_identifier(self) -> str:
        """A unique, schema-stable string identifying the event type."""

    @property
    @abstractmethod
    def priority(self) -> TracePriority:
        """The priority tier controlling survival under congestion."""

    # MARK: - Codability ---------------------------------------------------------

    def to_dict(self) -> dict:
        """Serialize payload state to a JSON-compatible dict.

        Default implementation reflects over dataclass fields. Bytes are base64-free
        here (events that carry binary payloads should override).
        """
        if is_dataclass(self):
            return {f.name: getattr(self, f.name) for f in fields(self)}
        raise NotImplementedError(
            f"{type(self).__name__} must implement to_dict() (not a dataclass)"
        )

    @classmethod
    def from_dict(cls, data: dict) -> "TraceableEvent":
        """Reconstruct a payload from a dict produced by :meth:`to_dict`."""
        return cls(**data)  # type: ignore[call-arg]

    def encode(self) -> bytes:
        """Encode the payload to JSON bytes. May raise to simulate an encode failure."""
        return json.dumps(self.to_dict(), sort_keys=True).encode("utf-8")

    @classmethod
    def decode(cls, data: bytes) -> "TraceableEvent":
        """Decode JSON bytes back into a payload instance."""
        return cls.from_dict(json.loads(data.decode("utf-8")))


@dataclass(frozen=True)
class TraceEvent:
    """The rich, generic in-memory envelope for a recorded event.

    Every event carries both a wall-clock ``timestamp`` and a monotonic per-run
    ``sequence``. ``sequence`` is authoritative; ``timestamp`` is for display and
    coarse range filtering only.
    """

    run_id: uuid.UUID
    context_id: str
    engine_name: str
    schema_version: int
    sequence: int
    span_id: Optional[str]
    parent_span_id: Optional[str]
    payload: TraceableEvent
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    timestamp: float = field(default_factory=time.time)


@dataclass(frozen=True)
class TraceEventRow:
    """A normalized, type-erased representation of a trace event for storage."""

    id: str
    run_id: str
    context_id: str
    priority: int
    sequence: int
    engine: Optional[str]
    span_id: Optional[str]
    parent_span_id: Optional[str]
    type: str
    payload: bytes
    timestamp: int


@dataclass
class RunRow:
    """A normalized representation of a trace run's metadata."""

    run_id: str
    context_id: str
    start_time: int
    end_time: int
    event_count: int
    fingerprint: str


@dataclass(frozen=True)
class AnyTraceableEvent(TraceableEvent):
    """A fully type-erased event that carries its identity and raw JSON payload."""

    type_identifier_value: str
    priority_value: int
    raw_json: str

    @property
    def type_identifier(self) -> str:
        return self.type_identifier_value

    @property
    def priority(self) -> TracePriority:
        try:
            return TracePriority(self.priority_value)
        except ValueError:
            return TracePriority.TELEMETRY

    def to_dict(self) -> dict:
        return {
            "type_identifier_value": self.type_identifier_value,
            "priority_value": self.priority_value,
            "raw_json": self.raw_json,
        }

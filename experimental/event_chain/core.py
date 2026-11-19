"""Experimental candidate-only event chain; no SDK or persistence integration."""
from dataclasses import dataclass
from hashlib import sha256
from hmac import compare_digest
import json
import re
from typing import Optional, Sequence, Tuple
from uuid import UUID

GENESIS_DOMAIN = b"DPROVENANCEKIT-EVENT-CHAIN-GENESIS-V1"
ENTRY_DOMAIN = b"DPROVENANCEKIT-EVENT-CHAIN-ENTRY-V1"
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


class ChainError(ValueError):
    """Stable single-fault candidate error code."""


def _utf8(value: str) -> bytes:
    if not isinstance(value, str):
        raise ChainError("INVALID_STRING")
    try:
        return value.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise ChainError("INVALID_STRING") from exc


def _uuid(value: str) -> UUID:
    if len(_utf8(value)) > 128:
        raise ChainError("IDENTIFIER_TOO_LONG")
    if _UUID.fullmatch(value) is None:
        raise ChainError("INVALID_IDENTIFIER")
    return UUID(value)


def _integer(value: int, *, signed: bool = False) -> bytes:
    if type(value) is not int:
        raise ChainError("INVALID_INTEGER")
    try:
        return value.to_bytes(8, "big", signed=signed)
    except OverflowError as exc:
        raise ChainError("INTEGER_OUT_OF_RANGE") from exc


def _string(value: str) -> bytes:
    raw = _utf8(value)
    return _integer(len(raw)) + raw


def _optional(value: Optional[str]) -> bytes:
    return b"\x00" if value is None else b"\x01" + _string(value)


def _hash(raw: bytes) -> bytes:
    if not isinstance(raw, bytes) or len(raw) != 32:
        raise ChainError("INVALID_HASH")
    return raw


def _pairs(pairs: list[Tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ChainError("INVALID_PAYLOAD_JSON")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ChainError("INVALID_PAYLOAD_JSON")


def _payload(value: str) -> bytes:
    raw = _string(value)
    try:
        # Number tokens are validated by the JSON grammar, without float coercion.
        parsed: object = json.loads(value, object_pairs_hook=_pairs,
                                    parse_constant=_reject_constant,
                                    parse_int=str, parse_float=str)
    except (ValueError, RecursionError) as exc:
        raise ChainError("INVALID_PAYLOAD_JSON") from exc
    if not isinstance(parsed, dict):
        raise ChainError("INVALID_PAYLOAD_JSON")
    return raw


@dataclass(frozen=True)
class Event:
    eventID: str
    runID: str
    contextID: str
    engineName: str
    schemaVersion: int
    eventSequence: int
    spanID: Optional[str]
    parentSpanID: Optional[str]
    typeIdentifier: str
    priority: int
    payloadJSON: str
    timestampUnixMicroseconds: int


@dataclass(frozen=True)
class Entry:
    chainIndex: int
    event: Event
    previousHash: bytes
    entryHash: bytes


@dataclass(frozen=True)
class Chain:
    runID: str
    head: bytes
    entries: Tuple[Entry, ...] = ()
    version: int = 1


def genesis_preimage(run_id: str) -> bytes:
    return GENESIS_DOMAIN + _uuid(run_id).bytes


def genesis(run_id: str) -> bytes:
    return sha256(genesis_preimage(run_id)).digest()


def event_bytes(event: Event) -> bytes:
    return b"".join((
        _uuid(event.eventID).bytes, _uuid(event.runID).bytes,
        _string(event.contextID), _string(event.engineName),
        _integer(event.schemaVersion, signed=True), _integer(event.eventSequence),
        _optional(event.spanID), _optional(event.parentSpanID),
        _string(event.typeIdentifier), _integer(event.priority, signed=True),
        _payload(event.payloadJSON), _integer(event.timestampUnixMicroseconds, signed=True),
    ))


def entry_preimage(index: int, previous: bytes, event: Event) -> bytes:
    return ENTRY_DOMAIN + _integer(1, signed=True) + _integer(index) + _hash(previous) + event_bytes(event)


def _event_invariants(event: Event, expected: UUID, seen: set[UUID],
                      last: Optional[int]) -> UUID:
    identity = _uuid(event.eventID)
    if _uuid(event.runID) != expected:
        raise ChainError("RUN_ID_MISMATCH")
    _integer(event.eventSequence)
    if identity in seen:
        raise ChainError("DUPLICATE_EVENT_ID")
    if last is not None and event.eventSequence <= last:
        raise ChainError("SEQUENCE_NONMONOTONIC")
    return identity


def verify(chain: Chain, expected_run_id: str, *, expected_count: Optional[int] = None,
           expected_head: Optional[bytes] = None, require_evidence: bool = False) -> None:
    """Raise on failure. External expected head/count must be independently trusted.

    This verifies consistency only; it does not authenticate the source of a commitment.
    """
    if type(chain.version) is not int or chain.version != 1:
        raise ChainError("UNSUPPORTED_VERSION")
    expected = _uuid(expected_run_id)
    if _uuid(chain.runID) != expected:
        raise ChainError("RUN_ID_MISMATCH")
    previous = genesis(expected_run_id)
    seen: set[UUID] = set()
    last: Optional[int] = None
    for index, entry in enumerate(chain.entries):
        _integer(entry.chainIndex)
        if entry.chainIndex != index:
            raise ChainError("CHAIN_INDEX_MISMATCH")
        identity = _event_invariants(entry.event, expected, seen, last)
        if not compare_digest(_hash(entry.previousHash), previous):
            raise ChainError("PREVIOUS_HASH_MISMATCH")
        calculated = sha256(entry_preimage(index, previous, entry.event)).digest()
        if not compare_digest(_hash(entry.entryHash), calculated):
            raise ChainError("ENTRY_HASH_MISMATCH")
        seen.add(identity)
        last = entry.event.eventSequence
        previous = calculated
    if not compare_digest(_hash(chain.head), previous):
        raise ChainError("HEAD_MISMATCH")
    if expected_count is not None:
        _integer(expected_count)
        if expected_count != len(chain.entries):
            raise ChainError("CLOSURE_MISMATCH")
    if expected_head is not None and not compare_digest(_hash(expected_head), previous):
        raise ChainError("CLOSURE_MISMATCH")
    if require_evidence and not chain.entries:
        raise ChainError("EMPTY_CHAIN")


class ChainBuilder:
    """Single-owner in-memory writer; never use as a trust boundary.

    Imported chains are fully verified once. Appending validates only the new
    event; snapshot creation materializes an immutable copy in linear time.
    Not thread-safe, not persistent, and not an authenticated commitment.
    """

    def __init__(self, run_id: str) -> None:
        self._run_id = run_id
        self._expected = _uuid(run_id)
        self._head = genesis(run_id)
        self._seen: set[UUID] = set()
        self._last: Optional[int] = None
        self._entries: list[Entry] = []

    @classmethod
    def from_chain(cls, chain: Chain, expected_run_id: str, *,
                   expected_count: Optional[int] = None,
                   expected_head: Optional[bytes] = None) -> "ChainBuilder":
        """Validate supplied state, optionally against external commitments."""
        verify(chain, expected_run_id, expected_count=expected_count,
               expected_head=expected_head)
        writer = cls(chain.runID)
        writer._entries = list(chain.entries)
        writer._seen = {_uuid(entry.event.eventID) for entry in chain.entries}
        writer._last = chain.entries[-1].event.eventSequence if chain.entries else None
        writer._head = chain.head
        return writer

    @property
    def head(self) -> bytes:
        return self._head

    @property
    def count(self) -> int:
        return len(self._entries)

    def append(self, event: Event) -> Entry:
        """Validate and hash before mutating state; amortized constant in entry count."""
        identity = _event_invariants(event, self._expected, self._seen, self._last)
        index = len(self._entries)
        digest = sha256(entry_preimage(index, self._head, event)).digest()
        entry = Entry(index, event, self._head, digest)
        self._seen.add(identity)
        try:
            self._entries.append(entry)
        except MemoryError:
            self._seen.remove(identity)
            raise
        self._last = event.eventSequence
        self._head = digest
        return entry

    def snapshot(self) -> Chain:
        """Create an immutable copy; call only when an entire snapshot is needed."""
        return Chain(self._run_id, self._head, tuple(self._entries))


def build(run_id: str, events: Sequence[Event]) -> Chain:
    """Build an immutable in-memory candidate in caller-supplied order."""
    writer = ChainBuilder(run_id)
    for event in events:
        writer.append(event)
    return writer.snapshot()


def append(chain: Chain, event: Event, expected_run_id: str) -> Chain:
    """Pure append; revalidate the existing chain before constructing a new value."""
    writer = ChainBuilder.from_chain(chain, expected_run_id)
    writer.append(event)
    return writer.snapshot()

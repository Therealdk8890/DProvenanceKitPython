"""Byte-exact, dependency-free DPK repository object protocol.

This is independent of the frozen trace and attestation formats. Decoding is
bounded and accepts only canonical encodings; no recorded value is executable.
"""

from __future__ import annotations

import hashlib
import struct
from typing import Any, Dict, Tuple

DOMAIN = "DPK-OBJECT-V1"
OBJECT_TYPES = frozenset({"blob", "workflow", "run", "commit", "evaluation", "signature"})
MAX_OBJECT_BYTES = 64 * 1024 * 1024
MAX_DEPTH = 64
MAX_ITEMS = 1000000


class RepositoryFormatError(ValueError):
    """An object or bundle is unsupported, malformed, or noncanonical."""


def _string(value: str) -> bytes:
    try:
        raw = value.encode("utf-8")
    except UnicodeError as exc:
        raise RepositoryFormatError("strings must be valid UTF-8") from exc
    return struct.pack(">Q", len(raw)) + raw


def _value(value: Any, depth: int = 0) -> bytes:
    if depth > MAX_DEPTH:
        raise RepositoryFormatError("object nesting limit exceeded")
    if value is None:
        return b"\x00"
    if value is False:
        return b"\x01"
    if value is True:
        return b"\x02"
    if type(value) is int:
        if not -(2**63) <= value < 2**63:
            raise RepositoryFormatError("integer outside signed 64-bit range")
        return b"\x03" + struct.pack(">q", value)
    if isinstance(value, str):
        return b"\x04" + _string(value)
    if isinstance(value, bytes):
        return b"\x05" + struct.pack(">Q", len(value)) + value
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_ITEMS:
            raise RepositoryFormatError("array item limit exceeded")
        return b"\x06" + struct.pack(">Q", len(value)) + b"".join(
            _value(item, depth + 1) for item in value
        )
    if isinstance(value, dict):
        if len(value) > MAX_ITEMS or any(not isinstance(k, str) for k in value):
            raise RepositoryFormatError("maps require string keys within item limit")
        keys = sorted(value, key=lambda k: k.encode("utf-8"))
        return b"\x07" + struct.pack(">Q", len(keys)) + b"".join(
            _string(key) + _value(value[key], depth + 1) for key in keys
        )
    raise RepositoryFormatError("unsupported value type; floats are not canonical metadata")


def encode_object(object_type: str, payload: Any) -> bytes:
    """Return domain + type + schema u64 + canonical tagged payload bytes."""
    if object_type not in OBJECT_TYPES:
        raise RepositoryFormatError("unsupported object type")
    encoded = _string(DOMAIN) + _string(object_type) + struct.pack(">Q", 1) + _value(payload)
    if len(encoded) > MAX_OBJECT_BYTES:
        raise RepositoryFormatError("object byte limit exceeded")
    return encoded


def object_id(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.offset = 0
        self.items = 0

    def read(self, size: int) -> bytes:
        if size < 0 or size > len(self.data) - self.offset:
            raise RepositoryFormatError("truncated object")
        result = self.data[self.offset:self.offset + size]
        self.offset += size
        return result

    def count(self) -> int:
        return int(struct.unpack(">Q", self.read(8))[0])

    def string(self) -> str:
        try:
            return self.read(self.count()).decode("utf-8")
        except UnicodeError as exc:
            raise RepositoryFormatError("invalid UTF-8") from exc

    def value(self, depth: int = 0) -> Any:
        self.items += 1
        if depth > MAX_DEPTH or self.items > MAX_ITEMS:
            raise RepositoryFormatError("object complexity limit exceeded")
        tag = self.read(1)[0]
        if tag == 0:
            return None
        if tag in (1, 2):
            return tag == 2
        if tag == 3:
            return int(struct.unpack(">q", self.read(8))[0])
        if tag == 4:
            return self.string()
        if tag == 5:
            return self.read(self.count())
        if tag in (6, 7):
            count = self.count()
            if count > MAX_ITEMS:
                raise RepositoryFormatError("container item limit exceeded")
            if tag == 6:
                return [self.value(depth + 1) for _ in range(count)]
            result: Dict[str, Any] = {}
            previous = None
            for _ in range(count):
                key = self.string()
                raw_key = key.encode("utf-8")
                if previous is not None and raw_key <= previous:
                    raise RepositoryFormatError("map keys must be unique and sorted by UTF-8 bytes")
                previous = raw_key
                result[key] = self.value(depth + 1)
            return result
        raise RepositoryFormatError("unknown value tag")


def decode_object(data: bytes) -> Tuple[str, Any]:
    """Decode a canonical object; reject trailing bytes and unsupported versions."""
    if not isinstance(data, bytes) or len(data) > MAX_OBJECT_BYTES:
        raise RepositoryFormatError("invalid object bytes or byte limit exceeded")
    reader = _Reader(data)
    if reader.string() != DOMAIN:
        raise RepositoryFormatError("unsupported object domain")
    kind = reader.string()
    if kind not in OBJECT_TYPES or reader.count() != 1:
        raise RepositoryFormatError("unsupported object type or version")
    payload = reader.value()
    if reader.offset != len(data):
        raise RepositoryFormatError("trailing object bytes")
    if encode_object(kind, payload) != data:
        raise RepositoryFormatError("noncanonical object")
    return kind, payload

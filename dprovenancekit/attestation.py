"""DPK-BINARY-V1 trace attestation (ECDSA P-256 DER) — software MVP.

Interoperates with Swift ``TraceAttestation`` / ``DPK-BINARY-V1`` (see upstream
``docs/ATTESTATION.md``). This module covers:

* Canonical length-prefixed binary encoding of an attestable trace
* SHA-256 trace digest
* Domain-separated signing envelope
* ECDSA P-256 (SHA-256) sign + verify with ASN.1 DER signatures
* Offline JSON document round-trip matching the Swift schema

Requires the optional ``cryptography`` extra::

    pip install dprovenancekit[crypto]

Secure Enclave keys, Keychain custody, and proof packs remain Swift-only.

Byte layout (``DPK-BINARY-V1``)
------------------------------
All integers are big-endian. Strings/Data are ``u64 length || utf-8/bytes``.
Optional strings are ``0x00`` (absent) or ``0x01 || string`` (present).
UUIDs are the 16-byte RFC 4122 representation (``uuid.UUID.bytes``).

Trace digest input (domain ``DPROVENANCEKIT-TRACE-ATTESTATION-V1``)::

    domain | schemaVersion:i64 | runID | contextID
    | eventCount:u64
    | for each event in array order:
        index:u64 | id | runID | contextID | engineName
        | schemaVersion:i64 | sequence:u64
        | spanID? | parentSpanID? | typeIdentifier
        | priority:i64 | payloadJSON | timestampUnixMicroseconds:i64
    | edgeCount:u64  (edges sorted by uppercase uuidString source, target, type)
    | for each sorted edge: sourceID | targetID | type.rawValue

Signing envelope (domain ``DPROVENANCEKIT-ATTESTATION-SIGNATURE-V1``)::

    domain | version:i64 | algorithm | canonicalization
    | runID | contextID | eventCount:u64 | edgeCount:u64
    | traceDigest (hex string) | issuedAtUnixMicroseconds:i64
    | keyID | publicKeyBase64

``keyID`` is lowercase hex SHA-256 of the P-256 X9.63 (uncompressed) public key.
CryptoKit ``signature(for:)`` / ``isValidSignature(_:for:)`` hash the envelope with
SHA-256 before ECDSA; this module matches that via ``ec.ECDSA(hashes.SHA256())``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import struct
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple, Union

from .edge import TraceEdge, TraceEdgeType

SCHEMA_VERSION = 1
ALGORITHM = "P256-SHA256"
CANONICALIZATION = "DPK-BINARY-V1"
TRACE_DOMAIN = "DPROVENANCEKIT-TRACE-ATTESTATION-V1"
SIGNATURE_DOMAIN = "DPROVENANCEKIT-ATTESTATION-SIGNATURE-V1"

_CRYPTO_HINT = (
    "ECDSA P-256 attestation requires the optional cryptography package. "
    "Install with: pip install dprovenancekit[crypto]"
)


def _require_crypto():
    try:
        from cryptography.hazmat.primitives.asymmetric import ec  # noqa: F401
        from cryptography.hazmat.primitives import hashes  # noqa: F401
        from cryptography.exceptions import InvalidSignature  # noqa: F401
    except ImportError as exc:  # pragma: no cover - exercised when extra missing
        raise ImportError(_CRYPTO_HINT) from exc


# ---------------------------------------------------------------------------
# Canonical binary writer
# ---------------------------------------------------------------------------


class _CanonicalBinaryWriter:
    __slots__ = ("_buf",)

    def __init__(self) -> None:
        self._buf = bytearray()

    @property
    def data(self) -> bytes:
        return bytes(self._buf)

    def append_u64(self, value: int) -> None:
        if value < 0 or value > 0xFFFFFFFFFFFFFFFF:
            raise ValueError("u64 out of range")
        self._buf += struct.pack(">Q", value)

    def append_i64(self, value: int) -> None:
        if value < -(1 << 63) or value > (1 << 63) - 1:
            raise ValueError("i64 out of range")
        self._buf += struct.pack(">q", value)

    def append_uuid(self, value: uuid.UUID) -> None:
        self._buf += value.bytes

    def append_bytes(self, value: bytes) -> None:
        self.append_u64(len(value))
        self._buf += value

    def append_string(self, value: str) -> None:
        self.append_bytes(value.encode("utf-8"))

    def append_optional_string(self, value: Optional[str]) -> None:
        if value is None:
            self._buf.append(0)
            return
        self._buf.append(1)
        self.append_string(value)


def _as_uuid(value: Union[uuid.UUID, str]) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))


def _uuid_sort_key(value: uuid.UUID) -> str:
    """Match Swift ``UUID.uuidString`` (uppercase hyphenated)."""
    return str(value).upper()


def _hex_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Data model (JSON field names match Swift Codable)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AttestableTraceEvent:
    id: uuid.UUID
    run_id: uuid.UUID
    context_id: str
    engine_name: str
    schema_version: int
    sequence: int
    span_id: Optional[str]
    parent_span_id: Optional[str]
    type_identifier: str
    priority: int
    payload_json: str
    timestamp_unix_microseconds: int

    def to_wire(self) -> Dict[str, Any]:
        wire: Dict[str, Any] = {
            "id": str(self.id).upper(),
            "runID": str(self.run_id).upper(),
            "contextID": self.context_id,
            "engineName": self.engine_name,
            "schemaVersion": self.schema_version,
            "sequence": self.sequence,
            "typeIdentifier": self.type_identifier,
            "priority": self.priority,
            "payloadJSON": self.payload_json,
            "timestampUnixMicroseconds": self.timestamp_unix_microseconds,
        }
        if self.span_id is not None:
            wire["spanID"] = self.span_id
        if self.parent_span_id is not None:
            wire["parentSpanID"] = self.parent_span_id
        return wire

    @classmethod
    def from_wire(cls, raw: Dict[str, Any]) -> "AttestableTraceEvent":
        return cls(
            id=_as_uuid(raw["id"]),
            run_id=_as_uuid(raw["runID"]),
            context_id=raw["contextID"],
            engine_name=raw["engineName"],
            schema_version=int(raw["schemaVersion"]),
            sequence=int(raw["sequence"]),
            span_id=raw.get("spanID"),
            parent_span_id=raw.get("parentSpanID"),
            type_identifier=raw["typeIdentifier"],
            priority=int(raw["priority"]),
            payload_json=raw["payloadJSON"],
            timestamp_unix_microseconds=int(raw["timestampUnixMicroseconds"]),
        )


@dataclass(frozen=True)
class AttestableTrace:
    run_id: uuid.UUID
    context_id: str
    events: Tuple[AttestableTraceEvent, ...]
    edges: Tuple[TraceEdge, ...] = ()

    def to_wire(self) -> Dict[str, Any]:
        return {
            "runID": str(self.run_id).upper(),
            "contextID": self.context_id,
            "events": [e.to_wire() for e in self.events],
            "edges": [
                {
                    "sourceID": str(edge.source_id).upper(),
                    "targetID": str(edge.target_id).upper(),
                    "type": edge.type.value,
                }
                for edge in self.edges
            ],
        }

    @classmethod
    def from_wire(cls, raw: Dict[str, Any]) -> "AttestableTrace":
        edges_raw = raw.get("edges") or []
        edges: List[TraceEdge] = []
        for item in edges_raw:
            edges.append(
                TraceEdge(
                    source_id=_as_uuid(item["sourceID"]),
                    target_id=_as_uuid(item["targetID"]),
                    type=TraceEdgeType(item["type"]),
                )
            )
        return cls(
            run_id=_as_uuid(raw["runID"]),
            context_id=raw["contextID"],
            events=tuple(AttestableTraceEvent.from_wire(e) for e in raw["events"]),
            edges=tuple(edges),
        )


@dataclass(frozen=True)
class TraceAttestation:
    version: int
    algorithm: str
    canonicalization: str
    run_id: uuid.UUID
    context_id: str
    event_count: int
    edge_count: int
    trace_digest: str
    issued_at_unix_microseconds: int
    key_id: str
    public_key_base64: str
    signature_base64: str

    def to_wire(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "algorithm": self.algorithm,
            "canonicalization": self.canonicalization,
            "runID": str(self.run_id).upper(),
            "contextID": self.context_id,
            "eventCount": self.event_count,
            "edgeCount": self.edge_count,
            "traceDigest": self.trace_digest,
            "issuedAtUnixMicroseconds": self.issued_at_unix_microseconds,
            "keyID": self.key_id,
            "publicKeyBase64": self.public_key_base64,
            "signatureBase64": self.signature_base64,
        }

    @classmethod
    def from_wire(cls, raw: Dict[str, Any]) -> "TraceAttestation":
        return cls(
            version=int(raw["version"]),
            algorithm=raw["algorithm"],
            canonicalization=raw["canonicalization"],
            run_id=_as_uuid(raw["runID"]),
            context_id=raw["contextID"],
            event_count=int(raw["eventCount"]),
            edge_count=int(raw["edgeCount"]),
            trace_digest=raw["traceDigest"],
            issued_at_unix_microseconds=int(raw["issuedAtUnixMicroseconds"]),
            key_id=raw["keyID"],
            public_key_base64=raw["publicKeyBase64"],
            signature_base64=raw["signatureBase64"],
        )


@dataclass(frozen=True)
class TraceAttestationDocument:
    trace: AttestableTrace
    attestation: TraceAttestation

    def to_wire(self) -> Dict[str, Any]:
        return {
            "trace": self.trace.to_wire(),
            "attestation": self.attestation.to_wire(),
        }

    def json_bytes(self, pretty: bool = True) -> bytes:
        """Encode the portable JSON document (sorted keys, UTF-8)."""
        if pretty:
            text = json.dumps(self.to_wire(), indent=2, sort_keys=True, ensure_ascii=False)
            return (text + "\n").encode("utf-8")
        return json.dumps(self.to_wire(), sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )

    @classmethod
    def from_wire(cls, raw: Dict[str, Any]) -> "TraceAttestationDocument":
        return cls(
            trace=AttestableTrace.from_wire(raw["trace"]),
            attestation=TraceAttestation.from_wire(raw["attestation"]),
        )

    @classmethod
    def decode_json(cls, data: Union[bytes, str]) -> "TraceAttestationDocument":
        if isinstance(data, bytes):
            raw = json.loads(data.decode("utf-8"))
        else:
            raw = json.loads(data)
        return cls.from_wire(raw)

    def verify(
        self, trusted_key_ids: Optional[Iterable[str]] = None
    ) -> "TraceAttestationVerification":
        return TraceAttestationVerifier.verify(
            self.attestation, self.trace, trusted_key_ids=trusted_key_ids
        )


# ---------------------------------------------------------------------------
# Canonicalization
# ---------------------------------------------------------------------------


def _sorted_edges(edges: Sequence[TraceEdge]) -> List[TraceEdge]:
    return sorted(
        edges,
        key=lambda e: (
            _uuid_sort_key(e.source_id),
            _uuid_sort_key(e.target_id),
            e.type.value,
        ),
    )


def canonical_trace_bytes(trace: AttestableTrace) -> bytes:
    """Return the DPK-BINARY-V1 bytes hashed for ``traceDigest``."""
    w = _CanonicalBinaryWriter()
    w.append_string(TRACE_DOMAIN)
    w.append_i64(SCHEMA_VERSION)
    w.append_uuid(trace.run_id)
    w.append_string(trace.context_id)
    w.append_u64(len(trace.events))
    for index, event in enumerate(trace.events):
        w.append_u64(index)
        w.append_uuid(event.id)
        w.append_uuid(event.run_id)
        w.append_string(event.context_id)
        w.append_string(event.engine_name)
        w.append_i64(event.schema_version)
        w.append_u64(event.sequence)
        w.append_optional_string(event.span_id)
        w.append_optional_string(event.parent_span_id)
        w.append_string(event.type_identifier)
        w.append_i64(event.priority)
        w.append_string(event.payload_json)
        w.append_i64(event.timestamp_unix_microseconds)
    sorted_edges = _sorted_edges(trace.edges)
    w.append_u64(len(sorted_edges))
    for edge in sorted_edges:
        w.append_uuid(edge.source_id)
        w.append_uuid(edge.target_id)
        w.append_string(edge.type.value)
    return w.data


def trace_digest_hex(trace: AttestableTrace) -> str:
    return _hex_digest(canonical_trace_bytes(trace))


def signing_payload_bytes(attestation: TraceAttestation) -> bytes:
    """Domain-separated envelope covered by the P-256 signature (pre-hash)."""
    w = _CanonicalBinaryWriter()
    w.append_string(SIGNATURE_DOMAIN)
    w.append_i64(attestation.version)
    w.append_string(attestation.algorithm)
    w.append_string(attestation.canonicalization)
    w.append_uuid(attestation.run_id)
    w.append_string(attestation.context_id)
    w.append_u64(attestation.event_count)
    w.append_u64(attestation.edge_count)
    w.append_string(attestation.trace_digest)
    w.append_i64(attestation.issued_at_unix_microseconds)
    w.append_string(attestation.key_id)
    w.append_string(attestation.public_key_base64)
    return w.data


def key_id_for_public_key_x963(public_key_x963: bytes) -> str:
    return _hex_digest(public_key_x963)


# ---------------------------------------------------------------------------
# Validation / verification result types
# ---------------------------------------------------------------------------


class TraceAttestationTrust(str, Enum):
    EMBEDDED_KEY_ONLY = "embeddedKeyOnly"
    TRUSTED_KEY = "trustedKey"


class TraceAttestationVerificationFailure(str, Enum):
    UNSUPPORTED_VERSION = "unsupportedVersion"
    UNSUPPORTED_ALGORITHM = "unsupportedAlgorithm"
    UNSUPPORTED_CANONICALIZATION = "unsupportedCanonicalization"
    RUN_ID_MISMATCH = "runIDMismatch"
    CONTEXT_ID_MISMATCH = "contextIDMismatch"
    EVENT_RUN_ID_MISMATCH = "eventRunIDMismatch"
    EVENT_CONTEXT_ID_MISMATCH = "eventContextIDMismatch"
    DUPLICATE_EVENT_ID = "duplicateEventID"
    NON_MONOTONIC_SEQUENCE = "nonMonotonicSequence"
    INVALID_PAYLOAD_JSON = "invalidPayloadJSON"
    EVENT_COUNT_MISMATCH = "eventCountMismatch"
    EDGE_COUNT_MISMATCH = "edgeCountMismatch"
    SELF_REFERENTIAL_EDGE = "selfReferentialEdge"
    DUPLICATE_EDGE = "duplicateEdge"
    DANGLING_EDGE = "danglingEdge"
    MALFORMED_DIGEST = "malformedDigest"
    DIGEST_MISMATCH = "digestMismatch"
    MALFORMED_PUBLIC_KEY = "malformedPublicKey"
    KEY_ID_MISMATCH = "keyIDMismatch"
    UNTRUSTED_KEY = "untrustedKey"
    MALFORMED_SIGNATURE = "malformedSignature"
    INVALID_SIGNATURE = "invalidSignature"


@dataclass(frozen=True)
class TraceAttestationVerification:
    is_valid: bool
    key_id: str
    trust: TraceAttestationTrust
    failure: Optional[TraceAttestationVerificationFailure]


class TraceAttestationError(Exception):
    """Raised when a trace cannot be signed because it fails structural checks."""


def _validate_payload_json(payload_json: str) -> bool:
    try:
        json.loads(payload_json)
        return True
    except (TypeError, ValueError, json.JSONDecodeError):
        return False


def _validate_for_signing(trace: AttestableTrace) -> None:
    event_ids: Set[uuid.UUID] = set()
    previous_sequence: Optional[int] = None
    for event in trace.events:
        if event.run_id != trace.run_id:
            raise TraceAttestationError("eventRunIDMismatch")
        if event.context_id != trace.context_id:
            raise TraceAttestationError("eventContextIDMismatch")
        if event.id in event_ids:
            raise TraceAttestationError("duplicateEventID")
        event_ids.add(event.id)
        if previous_sequence is not None and event.sequence <= previous_sequence:
            raise TraceAttestationError("nonMonotonicSequence")
        previous_sequence = event.sequence
        if not _validate_payload_json(event.payload_json):
            raise TraceAttestationError("invalidPayloadJSON")
    _validate_edges(trace.edges, event_ids)


def _validate_edges(edges: Sequence[TraceEdge], event_ids: Set[uuid.UUID]) -> None:
    seen: Set[Tuple[uuid.UUID, uuid.UUID, str]] = set()
    for edge in edges:
        if edge.source_id == edge.target_id:
            raise TraceAttestationError("selfReferentialEdge")
        key = (edge.source_id, edge.target_id, edge.type.value)
        if key in seen:
            raise TraceAttestationError("duplicateEdge")
        seen.add(key)

    anchored = set(event_ids)
    remaining = list(edges)
    grew = True
    while grew:
        grew = False
        still: List[TraceEdge] = []
        for edge in remaining:
            if edge.source_id in anchored or edge.target_id in anchored:
                anchored.add(edge.source_id)
                anchored.add(edge.target_id)
                grew = True
            else:
                still.append(edge)
        remaining = still
    if remaining:
        raise TraceAttestationError("danglingEdge")


def _verification_failure_for_trace(
    trace: AttestableTrace,
) -> Optional[TraceAttestationVerificationFailure]:
    try:
        _validate_for_signing(trace)
        return None
    except TraceAttestationError as exc:
        mapping = {
            "eventRunIDMismatch": TraceAttestationVerificationFailure.EVENT_RUN_ID_MISMATCH,
            "eventContextIDMismatch": TraceAttestationVerificationFailure.EVENT_CONTEXT_ID_MISMATCH,
            "duplicateEventID": TraceAttestationVerificationFailure.DUPLICATE_EVENT_ID,
            "nonMonotonicSequence": TraceAttestationVerificationFailure.NON_MONOTONIC_SEQUENCE,
            "invalidPayloadJSON": TraceAttestationVerificationFailure.INVALID_PAYLOAD_JSON,
            "selfReferentialEdge": TraceAttestationVerificationFailure.SELF_REFERENTIAL_EDGE,
            "duplicateEdge": TraceAttestationVerificationFailure.DUPLICATE_EDGE,
            "danglingEdge": TraceAttestationVerificationFailure.DANGLING_EDGE,
        }
        return mapping.get(str(exc), TraceAttestationVerificationFailure.INVALID_PAYLOAD_JSON)


# ---------------------------------------------------------------------------
# Keys + sign / verify
# ---------------------------------------------------------------------------


class SoftwareTraceAttestationKey:
    """Software P-256 signing key (X9.63 public key; raw 32-byte private scalar)."""

    def __init__(self, private_key=None) -> None:
        _require_crypto()
        from cryptography.hazmat.primitives.asymmetric import ec

        if private_key is None:
            self._private_key = ec.generate_private_key(ec.SECP256R1())
        else:
            self._private_key = private_key

    @classmethod
    def from_raw_representation(cls, raw: bytes) -> "SoftwareTraceAttestationKey":
        """Load from a 32-byte big-endian private scalar (CryptoKit rawRepresentation)."""
        _require_crypto()
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.ec import derive_private_key

        if len(raw) != 32:
            raise ValueError("P-256 rawRepresentation must be 32 bytes")
        scalar = int.from_bytes(raw, "big")
        private_key = derive_private_key(scalar, ec.SECP256R1())
        return cls(private_key=private_key)

    @property
    def raw_representation(self) -> bytes:
        numbers = self._private_key.private_numbers()
        return numbers.private_value.to_bytes(32, "big")

    @property
    def public_key_x963_representation(self) -> bytes:
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

        return self._private_key.public_key().public_bytes(
            Encoding.X962, PublicFormat.UncompressedPoint
        )

    def signature_der(self, data: bytes) -> bytes:
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives import hashes

        return self._private_key.sign(data, ec.ECDSA(hashes.SHA256()))


def attest(
    trace: AttestableTrace,
    key: SoftwareTraceAttestationKey,
    issued_at_unix_microseconds: Optional[int] = None,
) -> TraceAttestation:
    """Sign ``trace`` and return a ``TraceAttestation`` envelope."""
    import time

    _validate_for_signing(trace)
    public_key = key.public_key_x963_representation
    key_id = key_id_for_public_key_x963(public_key)
    digest = trace_digest_hex(trace)
    if issued_at_unix_microseconds is None:
        issued_at_unix_microseconds = int(time.time() * 1_000_000)

    unsigned = TraceAttestation(
        version=SCHEMA_VERSION,
        algorithm=ALGORITHM,
        canonicalization=CANONICALIZATION,
        run_id=trace.run_id,
        context_id=trace.context_id,
        event_count=len(trace.events),
        edge_count=len(trace.edges),
        trace_digest=digest,
        issued_at_unix_microseconds=issued_at_unix_microseconds,
        key_id=key_id,
        public_key_base64=base64.b64encode(public_key).decode("ascii"),
        signature_base64="",
    )
    signature = key.signature_der(signing_payload_bytes(unsigned))
    return TraceAttestation(
        version=unsigned.version,
        algorithm=unsigned.algorithm,
        canonicalization=unsigned.canonicalization,
        run_id=unsigned.run_id,
        context_id=unsigned.context_id,
        event_count=unsigned.event_count,
        edge_count=unsigned.edge_count,
        trace_digest=unsigned.trace_digest,
        issued_at_unix_microseconds=unsigned.issued_at_unix_microseconds,
        key_id=unsigned.key_id,
        public_key_base64=unsigned.public_key_base64,
        signature_base64=base64.b64encode(signature).decode("ascii"),
    )


def signed_document(
    trace: AttestableTrace,
    key: SoftwareTraceAttestationKey,
    issued_at_unix_microseconds: Optional[int] = None,
) -> TraceAttestationDocument:
    return TraceAttestationDocument(
        trace=trace,
        attestation=attest(trace, key, issued_at_unix_microseconds=issued_at_unix_microseconds),
    )


class TraceAttestationVerifier:
    @staticmethod
    def verify(
        attestation: TraceAttestation,
        trace: AttestableTrace,
        trusted_key_ids: Optional[Iterable[str]] = None,
    ) -> TraceAttestationVerification:
        _require_crypto()
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives import hashes
        from cryptography.exceptions import InvalidSignature

        trust = TraceAttestationTrust.EMBEDDED_KEY_ONLY
        trusted: Optional[Set[str]] = None
        if trusted_key_ids is not None:
            trusted = set(trusted_key_ids)

        def fail(reason: TraceAttestationVerificationFailure) -> TraceAttestationVerification:
            return TraceAttestationVerification(
                is_valid=False,
                key_id=attestation.key_id,
                trust=trust,
                failure=reason,
            )

        if attestation.version != SCHEMA_VERSION:
            return fail(TraceAttestationVerificationFailure.UNSUPPORTED_VERSION)
        if attestation.algorithm != ALGORITHM:
            return fail(TraceAttestationVerificationFailure.UNSUPPORTED_ALGORITHM)
        if attestation.canonicalization != CANONICALIZATION:
            return fail(TraceAttestationVerificationFailure.UNSUPPORTED_CANONICALIZATION)
        if attestation.run_id != trace.run_id:
            return fail(TraceAttestationVerificationFailure.RUN_ID_MISMATCH)
        if attestation.context_id != trace.context_id:
            return fail(TraceAttestationVerificationFailure.CONTEXT_ID_MISMATCH)

        structural = _verification_failure_for_trace(trace)
        if structural is not None:
            return fail(structural)

        if attestation.event_count != len(trace.events):
            return fail(TraceAttestationVerificationFailure.EVENT_COUNT_MISMATCH)
        if attestation.edge_count != len(trace.edges):
            return fail(TraceAttestationVerificationFailure.EDGE_COUNT_MISMATCH)

        digest_hex = attestation.trace_digest
        if len(digest_hex) != 64 or any(c not in "0123456789abcdef" for c in digest_hex):
            return fail(TraceAttestationVerificationFailure.MALFORMED_DIGEST)
        try:
            expected_digest = bytes.fromhex(digest_hex)
        except ValueError:
            return fail(TraceAttestationVerificationFailure.MALFORMED_DIGEST)
        if len(expected_digest) != 32:
            return fail(TraceAttestationVerificationFailure.MALFORMED_DIGEST)
        if hashlib.sha256(canonical_trace_bytes(trace)).digest() != expected_digest:
            return fail(TraceAttestationVerificationFailure.DIGEST_MISMATCH)

        try:
            public_key_data = base64.b64decode(attestation.public_key_base64)
            public_key = ec.EllipticCurvePublicKey.from_encoded_point(
                ec.SECP256R1(), public_key_data
            )
        except Exception:
            return fail(TraceAttestationVerificationFailure.MALFORMED_PUBLIC_KEY)

        actual_key_id = key_id_for_public_key_x963(public_key_data)
        if actual_key_id != attestation.key_id:
            return fail(TraceAttestationVerificationFailure.KEY_ID_MISMATCH)

        if trusted is not None:
            if attestation.key_id not in trusted:
                return fail(TraceAttestationVerificationFailure.UNTRUSTED_KEY)
            trust = TraceAttestationTrust.TRUSTED_KEY

        try:
            signature_data = base64.b64decode(attestation.signature_base64)
        except Exception:
            return fail(TraceAttestationVerificationFailure.MALFORMED_SIGNATURE)

        try:
            public_key.verify(
                signature_data,
                signing_payload_bytes(attestation),
                ec.ECDSA(hashes.SHA256()),
            )
        except InvalidSignature:
            return fail(TraceAttestationVerificationFailure.INVALID_SIGNATURE)
        except Exception:
            return fail(TraceAttestationVerificationFailure.MALFORMED_SIGNATURE)

        return TraceAttestationVerification(
            is_valid=True,
            key_id=attestation.key_id,
            trust=trust,
            failure=None,
        )

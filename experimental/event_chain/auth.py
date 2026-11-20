"""Candidate P-256 run closure. Optional cryptography dependency, no SDK integration."""
from dataclasses import dataclass
from hashlib import sha256
from hmac import compare_digest
from typing import Protocol, Tuple

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

from .core import (
    Chain, ChainError, Event, RunClose, _hash, _integer, _string, _uuid,
    close_preimage, event_bytes, verify, verify_close_binding,
)

EDGE_TYPES = frozenset({
    "derivedFrom", "influencedBy", "generatedFrom", "verifiedBy", "correctedBy", "informed",
})


@dataclass(frozen=True)
class Edge:
    sourceID: str
    targetID: str
    type: str


@dataclass(frozen=True)
class Trace:
    runID: str
    contextID: str
    events: Tuple[Event, ...]
    edges: Tuple[Edge, ...] = ()


@dataclass(frozen=True)
class SignedClose:
    close: RunClose
    publicKeyX963: bytes
    signatureDER: bytes


class SigningProvider(Protocol):
    @property
    def public_key_x963_representation(self) -> bytes: ...
    def signature_der(self, data: bytes) -> bytes: ...


def trace_bytes(chain: Chain, trace: Trace, expected_run_id: str) -> bytes:
    """Encode the existing DPK-BINARY-V1 full trace layout, checking coverage exactly.

    This experimental narrow adapter rejects malformed edges and context inconsistencies;
    it neither changes existing attestations nor implies capture completeness.
    """
    verify(chain, expected_run_id)
    run = _uuid(expected_run_id)
    if _uuid(trace.runID) != run or len(trace.events) != len(chain.entries):
        raise ChainError("TRACE_MISMATCH")
    parts = [_string("DPROVENANCEKIT-TRACE-ATTESTATION-V1"),
             _integer(1, signed=True), run.bytes, _string(trace.contextID),
             _integer(len(trace.events))]
    identities = set()
    for i, (event, entry) in enumerate(zip(trace.events, chain.entries)):
        if _uuid(event.runID) != run or event.contextID != trace.contextID:
            raise ChainError("TRACE_MISMATCH")
        encoded = event_bytes(event)
        if encoded != event_bytes(entry.event):
            raise ChainError("TRACE_MISMATCH")
        identities.add(_uuid(event.eventID))
        parts.extend((_integer(i), encoded))
    edges: set[Tuple[bytes, bytes, str]] = set()
    for edge in trace.edges:
        source, target = _uuid(edge.sourceID), _uuid(edge.targetID)
        row = (source.bytes, target.bytes, edge.type)
        if (source == target or source not in identities or target not in identities
                or edge.type not in EDGE_TYPES or row in edges):
            raise ChainError("INVALID_TRACE_EDGE")
        edges.add(row)
    parts.append(_integer(len(edges)))
    for source_bytes, target_bytes, kind in sorted(edges):
        parts.extend((source_bytes, target_bytes, _string(kind)))
    return b"".join(parts)


def trace_digest(chain: Chain, trace: Trace, expected_run_id: str) -> bytes:
    return sha256(trace_bytes(chain, trace, expected_run_id)).digest()


def _public_key(raw: bytes) -> ec.EllipticCurvePublicKey:
    if not isinstance(raw, bytes) or len(raw) != 65 or raw[0] != 4:
        raise ChainError("INVALID_PUBLIC_KEY")
    try:
        return ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw)
    except ValueError as exc:
        raise ChainError("INVALID_PUBLIC_KEY") from exc


def verify_authentication(
    chain: Chain, trace: Trace, signed: SignedClose, expected_run_id: str,
    *, trusted_key_id: bytes, require_evidence: bool = False,
) -> None:
    """Require an independently provisioned key ID. Never use embedded-only trust."""
    trusted = _hash(trusted_key_id)
    close = signed.close
    preimage = close_preimage(close)
    public = _public_key(signed.publicKeyX963)
    key_id = sha256(signed.publicKeyX963).digest()
    if not compare_digest(key_id, close.keyID):
        raise ChainError("KEY_ID_MISMATCH")
    if not compare_digest(key_id, trusted):
        raise ChainError("UNTRUSTED_KEY")
    if not isinstance(signed.signatureDER, bytes) or not 8 <= len(signed.signatureDER) <= 72:
        raise ChainError("INVALID_SIGNATURE")
    try:
        public.verify(signed.signatureDER, preimage, ec.ECDSA(hashes.SHA256()))
    except (InvalidSignature, ValueError) as exc:
        raise ChainError("INVALID_SIGNATURE") from exc
    verify_close_binding(chain, close, expected_run_id)
    if not compare_digest(trace_digest(chain, trace, expected_run_id), close.traceDigest):
        raise ChainError("TRACE_MISMATCH")
    if require_evidence and not chain.entries:
        raise ChainError("EMPTY_CHAIN")


def sign_close(
    chain: Chain, trace: Trace, expected_run_id: str, issued_at: int,
    provider: SigningProvider,
) -> SignedClose:
    """Sign validated coverage using the existing provider-shaped protocol."""
    raw = provider.public_key_x963_representation
    _public_key(raw)
    digest = trace_digest(chain, trace, expected_run_id)
    key_id = sha256(raw).digest()
    count = len(chain.entries)
    close = RunClose(expected_run_id, 1, count - 1 if count else None,
                     chain.head, count, digest, key_id, issued_at)
    signed = SignedClose(close, raw, provider.signature_der(close_preimage(close)))
    # A buggy or mismatched provider cannot return an invalid artifact as success.
    verify_authentication(chain, trace, signed, expected_run_id, trusted_key_id=key_id)
    return signed

"""DPK-BINARY-V1 attestation: round-trip + Swift golden-vector cross-verify."""

from __future__ import annotations

import base64
import copy
from pathlib import Path

import pytest

cryptography = pytest.importorskip("cryptography")

from dprovenancekit.attestation import (  # noqa: E402
    AttestableTrace,
    AttestableTraceEvent,
    SoftwareTraceAttestationKey,
    TraceAttestationDocument,
    TraceAttestationTrust,
    TraceAttestationVerificationFailure,
    attest,
    canonical_trace_bytes,
    signed_document,
    trace_digest_hex,
)
from dprovenancekit.edge import TraceEdge, TraceEdgeType  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "attestation-v1.json"
CONFORMANCE = (
    Path(__file__).resolve().parents[1] / "conformance" / "vectors" / "attestation-v1.json"
)


@pytest.fixture(scope="module")
def golden_doc() -> TraceAttestationDocument:
    return TraceAttestationDocument.decode_json(FIXTURE.read_bytes())


def test_fixture_files_match():
    assert FIXTURE.is_file()
    assert CONFORMANCE.is_file()
    assert FIXTURE.read_bytes() == CONFORMANCE.read_bytes()


def test_swift_golden_digest(golden_doc: TraceAttestationDocument):
    assert trace_digest_hex(golden_doc.trace) == golden_doc.attestation.trace_digest
    assert len(canonical_trace_bytes(golden_doc.trace)) > 0


def test_swift_golden_verifies(golden_doc: TraceAttestationDocument):
    result = golden_doc.verify()
    assert result.is_valid
    assert result.failure is None
    assert result.trust == TraceAttestationTrust.EMBEDDED_KEY_ONLY
    assert result.key_id == golden_doc.attestation.key_id


def test_swift_golden_trusted_key(golden_doc: TraceAttestationDocument):
    ok = golden_doc.verify(trusted_key_ids={golden_doc.attestation.key_id})
    assert ok.is_valid
    assert ok.trust == TraceAttestationTrust.TRUSTED_KEY

    bad = golden_doc.verify(trusted_key_ids={"0" * 64})
    assert not bad.is_valid
    assert bad.failure == TraceAttestationVerificationFailure.UNTRUSTED_KEY


def test_tamper_payload_fails(golden_doc: TraceAttestationDocument):
    wire = golden_doc.to_wire()
    wire = copy.deepcopy(wire)
    wire["trace"]["events"][0]["payloadJSON"] = '{"tampered":true}'
    tampered = TraceAttestationDocument.from_wire(wire)
    result = tampered.verify()
    assert not result.is_valid
    assert result.failure == TraceAttestationVerificationFailure.DIGEST_MISMATCH


def test_reorder_events_fails(golden_doc: TraceAttestationDocument):
    wire = copy.deepcopy(golden_doc.to_wire())
    wire["trace"]["events"][0], wire["trace"]["events"][1] = (
        wire["trace"]["events"][1],
        wire["trace"]["events"][0],
    )
    # Keep sequences as-is so structural validation may fail on non-monotonic
    # after swap; force monotonic by rewriting sequences to preserve order swap
    # as a pure digest change: restore monotonic sequences matching new order.
    for i, ev in enumerate(wire["trace"]["events"]):
        ev["sequence"] = i
    tampered = TraceAttestationDocument.from_wire(wire)
    result = tampered.verify()
    assert not result.is_valid
    assert result.failure == TraceAttestationVerificationFailure.DIGEST_MISMATCH


def test_round_trip_sign_verify():
    key = SoftwareTraceAttestationKey()
    run_id = __import__("uuid").UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    e1 = AttestableTraceEvent(
        id=__import__("uuid").UUID("11111111-1111-1111-1111-111111111111"),
        run_id=run_id,
        context_id="py-roundtrip",
        engine_name="Agent",
        schema_version=1,
        sequence=0,
        span_id="s1",
        parent_span_id=None,
        type_identifier="decision",
        priority=3,
        payload_json='{"decision":{"action":"Act"}}',
        timestamp_unix_microseconds=1_700_000_000_000_000,
    )
    e2 = AttestableTraceEvent(
        id=__import__("uuid").UUID("22222222-2222-2222-2222-222222222222"),
        run_id=run_id,
        context_id="py-roundtrip",
        engine_name="Agent",
        schema_version=1,
        sequence=1,
        span_id="s1",
        parent_span_id="s0",
        type_identifier="tool",
        priority=2,
        payload_json='{"toolExecution":{"toolName":"T","params":"p"}}',
        timestamp_unix_microseconds=1_700_000_000_000_100,
    )
    edge = TraceEdge(
        source_id=e1.id,
        target_id=e2.id,
        type=TraceEdgeType.DERIVED_FROM,
    )
    # Add a second edge with reverse sort order to exercise edge sorting.
    edge_b = TraceEdge(
        source_id=e2.id,
        target_id=e1.id,
        type=TraceEdgeType.INFLUENCED_BY,
    )
    trace = AttestableTrace(
        run_id=run_id,
        context_id="py-roundtrip",
        events=(e1, e2),
        edges=(edge_b, edge),  # deliberately unsorted input
    )
    doc = signed_document(trace, key, issued_at_unix_microseconds=1_700_000_000_123_456)
    assert doc.attestation.trace_digest == trace_digest_hex(trace)
    assert doc.attestation.key_id == __import__("hashlib").sha256(
        key.public_key_x963_representation
    ).hexdigest()

    result = doc.verify(trusted_key_ids={doc.attestation.key_id})
    assert result.is_valid
    assert result.trust == TraceAttestationTrust.TRUSTED_KEY

    # JSON round-trip preserves verify
    reloaded = TraceAttestationDocument.decode_json(doc.json_bytes())
    assert reloaded.verify().is_valid

    # Key rawRepresentation round-trip
    key2 = SoftwareTraceAttestationKey.from_raw_representation(key.raw_representation)
    assert key2.public_key_x963_representation == key.public_key_x963_representation
    doc2 = signed_document(trace, key2, issued_at_unix_microseconds=1_700_000_000_999_999)
    assert doc2.verify().is_valid
    assert doc2.attestation.key_id == doc.attestation.key_id


def test_fail_closed_self_loop():
    key = SoftwareTraceAttestationKey()
    run_id = __import__("uuid").UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    eid = __import__("uuid").UUID("11111111-1111-1111-1111-111111111111")
    event = AttestableTraceEvent(
        id=eid,
        run_id=run_id,
        context_id="x",
        engine_name="Agent",
        schema_version=1,
        sequence=0,
        span_id=None,
        parent_span_id=None,
        type_identifier="decision",
        priority=3,
        payload_json="{}",
        timestamp_unix_microseconds=1,
    )
    bad = AttestableTrace(
        run_id=run_id,
        context_id="x",
        events=(event,),
        edges=(TraceEdge(eid, eid, TraceEdgeType.DERIVED_FROM),),
    )
    with pytest.raises(Exception):
        attest(bad, key)


def test_fail_closed_bad_signature(golden_doc: TraceAttestationDocument):
    wire = copy.deepcopy(golden_doc.to_wire())
    # Flip a byte in the DER signature
    sig = bytearray(base64.b64decode(wire["attestation"]["signatureBase64"]))
    sig[-1] ^= 0x01
    wire["attestation"]["signatureBase64"] = base64.b64encode(sig).decode("ascii")
    doc = TraceAttestationDocument.from_wire(wire)
    result = doc.verify()
    assert not result.is_valid
    assert result.failure in (
        TraceAttestationVerificationFailure.INVALID_SIGNATURE,
        TraceAttestationVerificationFailure.MALFORMED_SIGNATURE,
    )

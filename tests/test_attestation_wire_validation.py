"""Strict malformed-wire tests for the DPK-BINARY-V1 attestation document."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

pytest.importorskip("cryptography")

from dprovenancekit.attestation import (  # noqa: E402
    TraceAttestationDocument,
    TraceAttestationVerificationFailure,
)

FIXTURE = Path(__file__).parent / "fixtures" / "attestation-v1.json"


@pytest.fixture
def wire():
    return copy.deepcopy(TraceAttestationDocument.decode_json(FIXTURE.read_bytes()).to_wire())


@pytest.mark.parametrize("edges", [None, "[]", {}])
def test_edges_must_be_present_as_array(wire, edges):
    wire["trace"]["edges"] = edges
    with pytest.raises((KeyError, ValueError, TypeError)):
        TraceAttestationDocument.from_wire(wire)


def test_missing_edges_is_rejected(wire):
    del wire["trace"]["edges"]
    with pytest.raises(KeyError):
        TraceAttestationDocument.from_wire(wire)


@pytest.mark.parametrize(
    ("section", "field"),
    [
        ("trace", "events.0.schemaVersion"),
        ("trace", "events.0.sequence"),
        ("trace", "events.0.priority"),
        ("trace", "events.0.timestampUnixMicroseconds"),
        ("attestation", "version"),
        ("attestation", "eventCount"),
        ("attestation", "edgeCount"),
        ("attestation", "issuedAtUnixMicroseconds"),
    ],
)
@pytest.mark.parametrize("bad_value", [True, "1"])
def test_integer_fields_reject_boolean_and_numeric_string(wire, section, field, bad_value):
    target = wire[section]
    for part in field.split("."):
        if part.isdigit():
            target = target[int(part)]
        elif part != field.split(".")[-1]:
            target = target[part]
        else:
            target[part] = bad_value
    with pytest.raises(ValueError, match="must be an integer"):
        TraceAttestationDocument.from_wire(wire)


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_document_json_rejects_nonstandard_numeric_constants(constant):
    data = ('{"unexpected":' + constant + '}').encode()
    with pytest.raises(ValueError, match="non-standard JSON numeric constant"):
        TraceAttestationDocument.decode_json(data)


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_payload_validation_rejects_nonstandard_numeric_constants(wire, constant):
    wire["trace"]["events"][0]["payloadJSON"] = '{"value":' + constant + '}'
    result = TraceAttestationDocument.from_wire(wire).verify()
    assert not result.is_valid
    assert result.failure == TraceAttestationVerificationFailure.INVALID_PAYLOAD_JSON


def test_trace_digest_requires_lowercase_hex(wire):
    wire["attestation"]["traceDigest"] = wire["attestation"]["traceDigest"].upper()
    result = TraceAttestationDocument.from_wire(wire).verify()
    assert not result.is_valid
    assert result.failure == TraceAttestationVerificationFailure.MALFORMED_DIGEST

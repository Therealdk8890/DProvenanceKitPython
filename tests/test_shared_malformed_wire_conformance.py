"""Shared malformed attestation wire cases mirrored by Swift XCTest.

Mutation fixture bytes must match docs/test-vectors/attestation-malformed-v1.json
in Therealdk8890/DProvenanceKit. Both use the same golden signed document.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

pytest.importorskip("cryptography")

from dprovenancekit.attestation import (  # noqa: E402
    TraceAttestationDocument,
    TraceAttestationVerificationFailure,
)

ROOT = Path(__file__).parent / "fixtures"
CORPUS = json.loads((ROOT / "attestation-malformed-v1.json").read_text(encoding="utf-8"))
assert CORPUS["schema"] == "dpk.attestation-malformed-v1"
assert CORPUS["base"] == "attestation-v1.json"
CASES = CORPUS["cases"]


def _mutate(wire: dict, name: str) -> None:
    trace = wire["trace"]
    attestation = wire["attestation"]
    if name == "uppercase_digest":
        attestation["traceDigest"] = attestation["traceDigest"].upper()
    elif name == "remove_edges":
        del trace["edges"]
    elif name == "null_edges":
        trace["edges"] = None
    elif name == "object_edges":
        trace["edges"] = {}
    elif name == "string_edges":
        trace["edges"] = "[]"
    elif name == "null_events":
        trace["events"] = None
    elif name == "event_schema_bool":
        trace["events"][0]["schemaVersion"] = True
    elif name == "event_sequence_string":
        trace["events"][0]["sequence"] = "1"
    elif name == "event_priority_bool":
        trace["events"][0]["priority"] = True
    elif name == "event_timestamp_string":
        trace["events"][0]["timestampUnixMicroseconds"] = "1"
    elif name == "envelope_version_bool":
        attestation["version"] = True
    elif name == "envelope_eventcount_string":
        attestation["eventCount"] = "5"
    elif name == "envelope_edgecount_bool":
        attestation["edgeCount"] = True
    elif name == "envelope_issued_string":
        attestation["issuedAtUnixMicroseconds"] = "1"
    else:
        raise AssertionError(f"unknown malformed-wire mutation: {name}")


@pytest.mark.parametrize("case", CASES, ids=[entry["id"] for entry in CASES])
def test_shared_malformed_wire_conformance(case):
    base = TraceAttestationDocument.decode_json((ROOT / CORPUS["base"]).read_bytes())
    assert base.verify().is_valid, "golden document itself must remain valid"
    wire = copy.deepcopy(base.to_wire())
    _mutate(wire, case["mutation"])
    if case["expected"] == "decodeRejected":
        with pytest.raises((KeyError, ValueError, TypeError)):
            TraceAttestationDocument.from_wire(wire)
    elif case["expected"] == "malformedDigest":
        result = TraceAttestationDocument.from_wire(wire).verify()
        assert not result.is_valid
        assert result.failure == TraceAttestationVerificationFailure.MALFORMED_DIGEST
    else:
        pytest.fail(f"unknown shared expected result: {case['expected']}")


def test_shared_malformed_corpus_is_unique_and_complete():
    ids = [entry["id"] for entry in CASES]
    mutations = [entry["mutation"] for entry in CASES]
    assert len(CASES) == 14
    assert len(ids) == len(set(ids))
    assert len(mutations) == len(set(mutations))

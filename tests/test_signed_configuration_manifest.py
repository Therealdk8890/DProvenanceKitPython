"""Shared-vector and fail-closed tests for signed configuration manifests."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from dprovenancekit.config_authorization import ConfigurationAuthorizationError
from dprovenancekit.signed_config_manifest import (
    TrustedConfigurationKey,
    verify_signed_configuration,
)

_VECTOR_PATH = (
    Path(__file__).resolve().parents[1]
    / "conformance"
    / "vectors"
    / "configuration_authorization_v1.json"
)


def _signature_vectors():
    return json.loads(_VECTOR_PATH.read_text(encoding="utf-8"))["signature"]


def _valid_configuration():
    root = json.loads(_VECTOR_PATH.read_text(encoding="utf-8"))
    return copy.deepcopy(root["valid"][0]["input"])


def _trusted_key():
    vector = _signature_vectors()["trusted_key"]
    return TrustedConfigurationKey(public_key=bytes.fromhex(vector["public_key_hex"]))


def test_shared_ed25519_manifest_vector_verifies():
    vector = _signature_vectors()
    assert vector["domain_separator_utf8"] == "DPK-CONFIG-MANIFEST-V1\n"
    assert (
        verify_signed_configuration(
            _valid_configuration(),
            vector["manifest"],
            {vector["manifest"]["key_id"]: _trusted_key()},
        )
        == vector["manifest"]["configuration_fingerprint"]
    )


@pytest.mark.parametrize(
    ("manifest_change", "key", "verification_time", "expected_code"),
    [
        ({"key_id": "unknown-key"}, None, None, "UNTRUSTED_KEY"),
        ({}, TrustedConfigurationKey(bytes.fromhex(_signature_vectors()["trusted_key"]["public_key_hex"]), revoked=True), None, "KEY_REVOKED"),
        ({}, TrustedConfigurationKey(bytes.fromhex(_signature_vectors()["trusted_key"]["public_key_hex"]), expires_at=1_700_000_000), 1_700_000_000, "KEY_EXPIRED"),
        ({}, TrustedConfigurationKey(bytes.fromhex(_signature_vectors()["trusted_key"]["public_key_hex"]), not_before=1_700_000_001), 1_700_000_000, "KEY_NOT_YET_VALID"),
        ({}, TrustedConfigurationKey(bytes.fromhex(_signature_vectors()["trusted_key"]["public_key_hex"]), expires_at=1_800_000_000), None, "KEY_POLICY_TIME_REQUIRED"),
        ({"signature": "0" * 128}, None, None, "SIGNATURE_INVALID"),
        ({"configuration_fingerprint": "0" * 64}, None, None, "CONFIGURATION_FINGERPRINT_MISMATCH"),
    ],
)
def test_manifest_and_trust_policy_fail_closed(
    manifest_change, key, verification_time, expected_code
):
    vector = _signature_vectors()
    manifest = dict(vector["manifest"])
    manifest.update(manifest_change)
    trusted_key_id = vector["manifest"]["key_id"]
    trusted = key or _trusted_key()
    with pytest.raises(ConfigurationAuthorizationError) as exc:
        verify_signed_configuration(
            _valid_configuration(),
            manifest,
            {trusted_key_id: trusted},
            verification_time=verification_time,
        )
    assert exc.value.code == expected_code


def test_unknown_manifest_fields_are_rejected():
    vector = _signature_vectors()
    manifest = dict(vector["manifest"], unreviewed_public_key=vector["trusted_key"]["public_key_hex"])
    with pytest.raises(ConfigurationAuthorizationError) as exc:
        verify_signed_configuration(
            _valid_configuration(),
            manifest,
            {manifest["key_id"]: _trusted_key()},
        )
    assert exc.value.code == "MANIFEST_UNKNOWN_FIELD"


def test_changed_configuration_cannot_reuse_signed_manifest():
    configuration = _valid_configuration()
    configuration["min_global_f1_bps"] = 9400
    vector = _signature_vectors()
    with pytest.raises(ConfigurationAuthorizationError) as exc:
        verify_signed_configuration(
            configuration,
            vector["manifest"],
            {vector["manifest"]["key_id"]: _trusted_key()},
        )
    assert exc.value.code == "CONFIGURATION_FINGERPRINT_MISMATCH"

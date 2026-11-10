"""Ed25519 verification for dpk-config-manifest-v1.

The Python core remains standard-library-only. This verifier loads the optional
`cryptography` backend lazily; install `dprovenancekit[crypto]` to use it.
"""

from __future__ import annotations

import hmac
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, NoReturn

from .config_authorization import (
    ConfigurationAuthorizationError,
    canonical_configuration_bytes,
    configuration_fingerprint,
)

_MANIFEST_VERSION = "dpk-config-manifest-v1"
_SIGNATURE_ALGORITHM = "Ed25519"
_DOMAIN_SEPARATOR = b"DPK-CONFIG-MANIFEST-V1\n"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_LOWER_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_LOWER_SIGNATURE = re.compile(r"^[0-9a-f]{128}$")
_MANIFEST_FIELDS = frozenset(
    {"manifest_version", "algorithm", "key_id", "configuration_fingerprint", "signature"}
)


@dataclass(frozen=True)
class TrustedConfigurationKey:
    """A public key supplied by an independently trusted deployment policy.

    `expires_at` is an exclusive Unix-seconds boundary. If either temporal
    bound is set, callers must supply the verification time explicitly.
    """

    public_key: bytes
    revoked: bool = False
    not_before: int | None = None
    expires_at: int | None = None


def _fail(code: str, message: str) -> NoReturn:
    raise ConfigurationAuthorizationError(code, message)


def verify_signed_configuration(
    configuration: Mapping[str, Any],
    manifest: Mapping[str, Any],
    trusted_keys: Mapping[str, TrustedConfigurationKey],
    *,
    verification_time: int | None = None,
) -> str:
    """Verify a dpk-config-manifest-v1 Ed25519 signature and return its fingerprint.

    The manifest never supplies its own trust anchor. `trusted_keys` must come
    from an independently trusted policy. Signature verification authenticates
    the signer relative to that policy; it does not prove configuration safety.
    """
    canonical = canonical_configuration_bytes(configuration)

    if not isinstance(manifest, Mapping):
        _fail("INVALID_MANIFEST_TYPE", "manifest must be a mapping")
    if any(not isinstance(key, str) for key in manifest):
        _fail("INVALID_MANIFEST_FIELD", "manifest field names must be strings")

    keys = set(manifest)
    missing = sorted(_MANIFEST_FIELDS - keys)
    unknown = sorted(keys - _MANIFEST_FIELDS)
    if missing:
        _fail("MANIFEST_MISSING_FIELD", f"missing manifest field: {missing[0]}")
    if unknown:
        _fail("MANIFEST_UNKNOWN_FIELD", f"unknown manifest field: {unknown[0]}")

    if manifest["manifest_version"] != _MANIFEST_VERSION:
        _fail("UNSUPPORTED_MANIFEST_VERSION", "unsupported signed manifest version")
    if manifest["algorithm"] != _SIGNATURE_ALGORITHM:
        _fail("UNSUPPORTED_SIGNATURE_ALGORITHM", "manifest algorithm must be Ed25519")

    key_id = manifest["key_id"]
    if not isinstance(key_id, str) or not _IDENTIFIER.fullmatch(key_id):
        _fail("INVALID_KEY_ID", "manifest key_id must be a valid ASCII identifier")

    expected_fingerprint = manifest["configuration_fingerprint"]
    if not isinstance(expected_fingerprint, str) or not _LOWER_SHA256.fullmatch(
        expected_fingerprint
    ):
        _fail(
            "INVALID_MANIFEST_FINGERPRINT",
            "manifest fingerprint must be 64 lowercase hexadecimal characters",
        )

    signature_hex = manifest["signature"]
    if not isinstance(signature_hex, str) or not _LOWER_SIGNATURE.fullmatch(signature_hex):
        _fail(
            "INVALID_SIGNATURE_ENCODING",
            "Ed25519 signature must be 128 lowercase hexadecimal characters",
        )

    actual_fingerprint = configuration_fingerprint(configuration)
    if not hmac.compare_digest(actual_fingerprint, expected_fingerprint):
        _fail(
            "CONFIGURATION_FINGERPRINT_MISMATCH",
            "manifest fingerprint does not match the validated configuration",
        )

    if not isinstance(trusted_keys, Mapping):
        _fail("INVALID_TRUST_POLICY", "trusted_keys must be an independently supplied mapping")
    trusted_key = trusted_keys.get(key_id)
    if trusted_key is None:
        _fail("UNTRUSTED_KEY", "manifest key_id is not present in the trusted key policy")
    if not isinstance(trusted_key, TrustedConfigurationKey):
        _fail("INVALID_TRUSTED_KEY", "trusted key policy entry has an invalid type")
    if not isinstance(trusted_key.public_key, bytes) or len(trusted_key.public_key) != 32:
        _fail("INVALID_TRUSTED_KEY", "trusted Ed25519 public key must be exactly 32 bytes")
    if type(trusted_key.revoked) is not bool:
        _fail("INVALID_TRUSTED_KEY", "trusted key revoked flag must be boolean")
    if trusted_key.revoked:
        _fail("KEY_REVOKED", "manifest signing key is revoked by trusted policy")

    temporal_bounds = (trusted_key.not_before, trusted_key.expires_at)
    for bound in temporal_bounds:
        if bound is not None and type(bound) is not int:
            _fail("INVALID_TRUSTED_KEY", "trusted key validity bounds must be integer Unix seconds")
    if any(bound is not None for bound in temporal_bounds):
        if type(verification_time) is not int:
            _fail(
                "KEY_POLICY_TIME_REQUIRED",
                "verification_time is required when trusted key policy defines validity bounds",
            )
        if trusted_key.not_before is not None and verification_time < trusted_key.not_before:
            _fail("KEY_NOT_YET_VALID", "manifest signing key is not yet valid")
        if trusted_key.expires_at is not None and verification_time >= trusted_key.expires_at:
            _fail("KEY_EXPIRED", "manifest signing key has expired")

    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as exc:
        raise ConfigurationAuthorizationError(
            "CRYPTO_BACKEND_UNAVAILABLE",
            "Ed25519 verification requires the optional cryptography dependency; "
            "install dprovenancekit[crypto]",
        ) from exc

    try:
        Ed25519PublicKey.from_public_bytes(trusted_key.public_key).verify(
            bytes.fromhex(signature_hex),
            _DOMAIN_SEPARATOR + canonical,
        )
    except InvalidSignature as exc:
        raise ConfigurationAuthorizationError(
            "SIGNATURE_INVALID", "Ed25519 signature verification failed"
        ) from exc
    except ValueError as exc:
        raise ConfigurationAuthorizationError(
            "INVALID_TRUSTED_KEY", "trusted Ed25519 public key is invalid"
        ) from exc

    return actual_fingerprint

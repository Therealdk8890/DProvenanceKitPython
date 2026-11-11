"""Deterministic configuration fingerprinting and pinned authorization (experimental v1).

This module is intentionally separate from AlignmentExecutionContract.profile_hash:
that legacy hash identifies an execution profile, while this contract authorizes a
closed, versioned policy document against an independently supplied fingerprint.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from typing import Any


_SCHEMA_VERSION = "dpk-config-v1"
_REQUIRED_FIELDS = frozenset(
    {
        "algorithm",
        "min_global_f1_bps",
        "min_node_conformance_bps",
        "schema_version",
        "weight_hierarchy_bps",
        "weight_payload_bps",
        "weight_type_bps",
    }
)
_THRESHOLD_FIELDS = frozenset({"min_global_f1_bps", "min_node_conformance_bps"})
_WEIGHT_FIELDS = frozenset(
    {"weight_hierarchy_bps", "weight_payload_bps", "weight_type_bps"}
)
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_LOWER_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ConfigurationAuthorizationError(ValueError):
    """A deterministic configuration validation or authorization failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _reject_constant(value: str) -> None:
    raise ConfigurationAuthorizationError(
        "NON_INTEGER_NUMERIC_VALUE",
        f"non-standard JSON numeric constant is forbidden: {value}",
    )


def _reject_float(value: str) -> None:
    raise ConfigurationAuthorizationError(
        "NON_INTEGER_NUMERIC_VALUE",
        f"floating-point JSON number is forbidden: {value}",
    )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ConfigurationAuthorizationError(
                "DUPLICATE_FIELD", f"duplicate configuration field: {key}"
            )
        result[key] = value
    return result


def parse_configuration_json(document: str | bytes) -> dict[str, Any]:
    """Parse untrusted JSON strictly, rejecting duplicate keys, floats and NaN/Infinity."""
    if isinstance(document, bytes):
        try:
            text = document.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ConfigurationAuthorizationError(
                "INVALID_UTF8", "configuration JSON must be valid UTF-8"
            ) from exc
    elif isinstance(document, str):
        text = document
    else:
        raise ConfigurationAuthorizationError(
            "INVALID_DOCUMENT_TYPE", "configuration document must be str or bytes"
        )

    if text.startswith("\ufeff"):
        raise ConfigurationAuthorizationError(
            "BYTE_ORDER_MARK_FORBIDDEN", "configuration JSON must not contain a BOM"
        )
    try:
        value = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
        )
    except ConfigurationAuthorizationError:
        raise
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ConfigurationAuthorizationError(
            "INVALID_JSON", "configuration document is not valid JSON"
        ) from exc
    if not isinstance(value, dict):
        raise ConfigurationAuthorizationError(
            "INVALID_CONFIGURATION_TYPE", "configuration must be a JSON object"
        )
    return value


def canonical_configuration_bytes(configuration: Mapping[str, Any]) -> bytes:
    """Validate dpk-config-v1 and return its exact canonical UTF-8 JSON bytes."""
    if not isinstance(configuration, Mapping):
        raise ConfigurationAuthorizationError(
            "INVALID_CONFIGURATION_TYPE", "configuration must be a mapping"
        )
    if any(not isinstance(key, str) for key in configuration):
        raise ConfigurationAuthorizationError(
            "INVALID_FIELD_NAME", "configuration field names must be strings"
        )

    keys = set(configuration.keys())
    missing = sorted(_REQUIRED_FIELDS - keys)
    unknown = sorted(keys - _REQUIRED_FIELDS)
    if missing:
        raise ConfigurationAuthorizationError(
            "MISSING_FIELD", f"missing required configuration field: {missing[0]}"
        )
    if unknown:
        raise ConfigurationAuthorizationError(
            "UNKNOWN_FIELD", f"unknown configuration field: {unknown[0]}"
        )

    algorithm = configuration["algorithm"]
    if (
        not isinstance(algorithm, str)
        or not algorithm.isascii()
        or not _IDENTIFIER.fullmatch(algorithm)
    ):
        raise ConfigurationAuthorizationError(
            "INVALID_IDENTIFIER", "algorithm must be a non-empty ASCII identifier"
        )
    if configuration["schema_version"] != _SCHEMA_VERSION:
        raise ConfigurationAuthorizationError(
            "UNSUPPORTED_SCHEMA_VERSION",
            f"schema_version must be {_SCHEMA_VERSION}",
        )

    normalized: dict[str, Any] = {
        "algorithm": algorithm,
        "schema_version": _SCHEMA_VERSION,
    }
    for field in sorted(_THRESHOLD_FIELDS | _WEIGHT_FIELDS):
        value = configuration[field]
        # bool is a subclass of int in Python; exact type checking is deliberate.
        if type(value) is not int:
            raise ConfigurationAuthorizationError(
                "NON_INTEGER_NUMERIC_VALUE", f"{field} must be an integer"
            )
        if not 0 <= value <= 10000:
            code = (
                "WEIGHT_OUT_OF_RANGE"
                if field in _WEIGHT_FIELDS
                else "THRESHOLD_OUT_OF_RANGE"
            )
            raise ConfigurationAuthorizationError(
                code, f"{field} must be between 0 and 10000 basis points"
            )
        normalized[field] = value

    weight_total = sum(normalized[field] for field in _WEIGHT_FIELDS)
    if weight_total != 10000:
        raise ConfigurationAuthorizationError(
            "WEIGHTS_SUM_INVALID",
            f"alignment weights must sum to exactly 10000 basis points (got {weight_total})",
        )

    # The schema is closed and every value is an ASCII string or an integer, so
    # sorted compact JSON is byte-stable across conforming implementations.
    return json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def configuration_fingerprint(configuration: Mapping[str, Any]) -> str:
    """Return lowercase SHA-256 of validated canonical dpk-config-v1 bytes."""
    return hashlib.sha256(canonical_configuration_bytes(configuration)).hexdigest()


def verify_pinned_configuration(
    configuration: Mapping[str, Any], expected_fingerprint: str
) -> str:
    """Fail closed unless the computed fingerprint matches an external pinned value.

    The expected fingerprint must be supplied by a separately reviewed trust policy;
    it is never read from the candidate configuration itself. Returns the verified
    fingerprint on success.
    """
    if not isinstance(expected_fingerprint, str) or not _LOWER_SHA256.fullmatch(
        expected_fingerprint
    ):
        raise ConfigurationAuthorizationError(
            "INVALID_EXPECTED_FINGERPRINT",
            "expected fingerprint must be 64 lowercase hexadecimal characters",
        )
    actual = configuration_fingerprint(configuration)
    if not hmac.compare_digest(actual, expected_fingerprint):
        raise ConfigurationAuthorizationError(
            "FINGERPRINT_MISMATCH",
            "configuration fingerprint does not match the independently pinned value",
        )
    return actual

"""Conformance tests for deterministic configuration authorization v1."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from dprovenancekit.config_authorization import (
    ConfigurationAuthorizationError,
    canonical_configuration_bytes,
    configuration_fingerprint,
    parse_configuration_json,
    verify_pinned_configuration,
)


_VECTOR_PATH = (
    Path(__file__).resolve().parents[1]
    / "conformance"
    / "vectors"
    / "configuration_authorization_v1.json"
)


def _vectors():
    return json.loads(_VECTOR_PATH.read_text(encoding="utf-8"))


def test_valid_golden_vectors_match_exact_bytes_and_sha256():
    vectors = _vectors()
    for vector in vectors["valid"]:
        canonical = canonical_configuration_bytes(vector["input"])
        assert canonical == vector["canonical_utf8"].encode("utf-8")
        assert configuration_fingerprint(vector["input"]) == vector["sha256"]


def test_reordered_input_keys_have_identical_canonical_bytes():
    first, second = _vectors()["valid"]
    assert canonical_configuration_bytes(first["input"]) == canonical_configuration_bytes(
        second["input"]
    )


@pytest.mark.parametrize("case", _vectors()["invalid"], ids=lambda case: case["id"])
def test_invalid_golden_vectors_fail_before_fingerprinting(case):
    configuration = copy.deepcopy(_vectors()["valid"][0]["input"])
    if "mutation" in case:
        configuration.update(case["mutation"])
    if "remove" in case:
        configuration.pop(case["remove"])
    if "add" in case:
        configuration.update(case["add"])

    with pytest.raises(ConfigurationAuthorizationError) as exc:
        canonical_configuration_bytes(configuration)
    assert exc.value.code == case["error"]


def test_changed_valid_configuration_has_a_different_fingerprint():
    vectors = _vectors()
    original = copy.deepcopy(vectors["valid"][0]["input"])
    changed = copy.deepcopy(original)
    changed.update(vectors["changed_configuration"][0]["mutation"])
    assert configuration_fingerprint(changed) != configuration_fingerprint(original)


def test_pinned_fingerprint_accepts_only_independently_matching_value():
    vector = _vectors()["valid"][0]
    assert (
        verify_pinned_configuration(vector["input"], vector["sha256"])
        == vector["sha256"]
    )
    with pytest.raises(ConfigurationAuthorizationError) as exc:
        verify_pinned_configuration(vector["input"], "0" * 64)
    assert exc.value.code == "FINGERPRINT_MISMATCH"


@pytest.mark.parametrize(
    ("document", "expected_code"),
    [
        ('{"algorithm":"structural-alignment-v1","algorithm":"other"}', "DUPLICATE_FIELD"),
        ('{"weight_type_bps": 1.0}', "NON_INTEGER_NUMERIC_VALUE"),
        ('{"weight_type_bps": NaN}', "NON_INTEGER_NUMERIC_VALUE"),
        ('{"weight_type_bps": Infinity}', "NON_INTEGER_NUMERIC_VALUE"),
        ("\ufeff{}", "BYTE_ORDER_MARK_FORBIDDEN"),
    ],
)
def test_strict_json_parser_rejects_ambiguous_or_noncanonical_input(
    document, expected_code
):
    with pytest.raises(ConfigurationAuthorizationError) as exc:
        parsed = parse_configuration_json(document)
        canonical_configuration_bytes(parsed)
    assert exc.value.code == expected_code


def test_algorithm_must_be_ascii():
    configuration = copy.deepcopy(_vectors()["valid"][0]["input"])
    configuration["algorithm"] = "café"
    with pytest.raises(ConfigurationAuthorizationError) as exc:
        canonical_configuration_bytes(configuration)
    assert exc.value.code == "INVALID_IDENTIFIER"


def test_boolean_is_not_accepted_as_integer_basis_points():
    configuration = copy.deepcopy(_vectors()["valid"][0]["input"])
    configuration["weight_type_bps"] = True
    with pytest.raises(ConfigurationAuthorizationError) as exc:
        canonical_configuration_bytes(configuration)
    assert exc.value.code == "NON_INTEGER_NUMERIC_VALUE"


def test_expected_fingerprint_must_be_lowercase_sha256():
    configuration = _vectors()["valid"][0]["input"]
    with pytest.raises(ConfigurationAuthorizationError) as exc:
        verify_pinned_configuration(configuration, "A" * 64)
    assert exc.value.code == "INVALID_EXPECTED_FINGERPRINT"

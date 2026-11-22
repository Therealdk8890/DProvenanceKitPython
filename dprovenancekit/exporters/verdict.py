"""A vendor-neutral, serializable gate verdict and its OpenTelemetry attribute mapping.

:class:`GateVerdict` is the single value every exporter in this package consumes. Build
it from an in-process :class:`~dprovenancekit.testing.RegressionReport`
(:meth:`GateVerdict.from_report`) or from the JSON that ``dpk gate --json`` prints in CI
(:meth:`GateVerdict.from_gate_json`). :meth:`GateVerdict.to_attributes` then flattens it
into OpenTelemetry-legal attributes under the ``dpk.*`` namespace.

The attribute names and value shapes are a published contract: they are specified in
``conformance/OTEL_GENAI_MAPPING_v1.md`` and pinned by
``conformance/vectors/otel_verdict_attributes.json``. Change them only by regenerating
those vectors in a reviewed commit.

Standard library only. Nothing here imports OpenTelemetry or any vendor SDK.
"""

from __future__ import annotations

import string
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

#: Version of the ``dpk.*`` verdict attribute contract emitted by :meth:`to_attributes`.
VERDICT_SCHEMA = "dpk.gate.verdict.v1"

#: Upper bound for free-text attributes (reasoning / summary). OTel backends commonly cap
#: attribute values (Datadog, Langfuse and Phoenix all truncate long strings), so the SDK
#: truncates deterministically instead of letting each backend cut at a different place.
MAX_TEXT_LENGTH = 1024

#: Upper bound on list-valued step attributes. A pathological diff (thousands of added
#: steps) must not produce an unbounded span; the full list stays in the gate JSON.
MAX_LIST_ITEMS = 64

_SDK_NAME = "dprovenancekit-python"

#: ``gen_ai.evaluation.name`` used for the standard GenAI evaluation event.
EVALUATION_NAME = "dpk_gate"

AttributeValue = Union[str, bool, int, float, List[str]]


def _truncate(text: str, limit: int = MAX_TEXT_LENGTH) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


def _sdk_version() -> str:
    try:
        from .. import __version__

        return str(__version__)
    except Exception:  # pragma: no cover - defensive: never fail an export on metadata
        return "unknown"


def _string_list(values: Optional[Sequence[Any]]) -> Tuple[str, ...]:
    if not values:
        return ()
    return tuple(str(v) for v in values)


@dataclass(frozen=True)
class GateVerdict:
    """The outcome of one DProvenanceKit gate check, ready to attach to a trace.

    Only ``passed`` is required. Every other field is optional so a verdict can be built
    from partial CI output; absent optional fields are simply omitted from attributes.

    Attributes:
        passed: the gate verdict.
        regression_level: engine-assessed severity (``none``/``low``/``medium``/``high``).
        strength: regression-risk strength in ``[0, 1]``.
        fingerprint_match: whether golden and candidate structural fingerprints match.
        golden_fingerprint / candidate_fingerprint: the run fingerprints (TRACE_SPEC §5).
        golden_run_id / candidate_run_id: DProvenanceKit run ids that were compared. For
            runs imported with ``dpk ingest`` / ``dpk init --from-otel`` the run id is the
            OTel trace id rendered as a UUID, which is what lets a verdict be joined back
            to the originating trace.
        max_regression_level / allow_divergent_steps: the gate policy that was applied.
        profile: alignment profile name, when known.
        removed_steps / added_steps / divergent_steps: step ``type_identifier``s.
        reasoning: the engine's explanation.
        evidence: optional evidence references (for example an attestation digest or a
            signer key id). Keys become ``dpk.evidence.<key>``; values must be strings.
    """

    passed: bool
    regression_level: Optional[str] = None
    strength: Optional[float] = None
    fingerprint_match: Optional[bool] = None
    golden_fingerprint: Optional[str] = None
    candidate_fingerprint: Optional[str] = None
    golden_run_id: Optional[str] = None
    candidate_run_id: Optional[str] = None
    max_regression_level: Optional[str] = None
    allow_divergent_steps: Optional[bool] = None
    profile: Optional[str] = None
    removed_steps: Tuple[str, ...] = ()
    added_steps: Tuple[str, ...] = ()
    divergent_steps: Tuple[str, ...] = ()
    reasoning: Optional[str] = None
    evidence: Tuple[Tuple[str, str], ...] = field(default_factory=tuple)

    # ── construction ────────────────────────────────────────────────────────────

    @classmethod
    def from_report(
        cls,
        report: Any,
        *,
        golden_run_id: Optional[Any] = None,
        candidate_run_id: Optional[Any] = None,
        profile: Optional[str] = None,
        evidence: Optional[Mapping[str, str]] = None,
    ) -> "GateVerdict":
        """Build a verdict from a :class:`~dprovenancekit.testing.RegressionReport`."""
        return cls(
            passed=bool(report.passed),
            regression_level=_enum_value(report.regression_level),
            strength=float(report.strength),
            fingerprint_match=bool(report.fingerprint_match),
            golden_fingerprint=str(report.golden_fingerprint),
            candidate_fingerprint=str(report.candidate_fingerprint),
            golden_run_id=_optional_str(golden_run_id),
            candidate_run_id=_optional_str(candidate_run_id),
            max_regression_level=_enum_value(report.max_regression_level),
            allow_divergent_steps=bool(report.allow_divergent_steps),
            profile=profile,
            removed_steps=_string_list(report.removed_steps),
            added_steps=_string_list(report.added_steps),
            divergent_steps=_string_list(report.divergent_steps),
            reasoning=report.reasoning or None,
            evidence=_evidence_items(evidence),
        )

    @classmethod
    def from_gate_json(
        cls,
        data: Mapping[str, Any],
        *,
        evidence: Optional[Mapping[str, str]] = None,
    ) -> "GateVerdict":
        """Build a verdict from the object ``dpk gate --json`` prints.

        Accepts output from older SDKs too: the run-id and profile keys are optional and
        the step lists are derived from ``steps_by_change`` when present.
        """
        if "passed" not in data:
            raise ValueError("gate JSON is missing required field 'passed'")
        steps = data.get("steps_by_change") or {}
        if not isinstance(steps, Mapping):
            steps = {}
        divergent: List[str] = []
        for kind in ("removed", "added", "reordered", "ambiguous"):
            divergent.extend(str(s) for s in steps.get(kind, []) or [])
        strength = data.get("strength")
        return cls(
            passed=bool(data["passed"]),
            regression_level=_optional_str(data.get("regression_level")),
            strength=float(strength) if strength is not None else None,
            fingerprint_match=_optional_bool(data.get("fingerprint_match")),
            golden_fingerprint=_optional_str(data.get("golden_fingerprint")),
            candidate_fingerprint=_optional_str(data.get("candidate_fingerprint")),
            golden_run_id=_optional_str(data.get("golden_run_id")),
            candidate_run_id=_optional_str(data.get("candidate_run_id")),
            max_regression_level=_optional_str(data.get("max_regression_level")),
            allow_divergent_steps=_optional_bool(data.get("allow_divergent_steps")),
            profile=_optional_str(data.get("profile")),
            removed_steps=_string_list(steps.get("removed")),
            added_steps=_string_list(steps.get("added")),
            divergent_steps=tuple(divergent),
            reasoning=_optional_str(data.get("reasoning")) or None,
            evidence=_evidence_items(evidence),
        )

    # ── views ───────────────────────────────────────────────────────────────────

    @property
    def label(self) -> str:
        """``"pass"`` or ``"fail"``: the categorical form most backends display."""
        return "pass" if self.passed else "fail"

    @property
    def score(self) -> float:
        """``1.0`` for a pass, ``0.0`` for a fail: the numeric form for score APIs."""
        return 1.0 if self.passed else 0.0

    def summary(self) -> str:
        """A one-line, human-readable explanation suitable for a comment/explanation."""
        parts = ["DProvenanceKit gate: " + ("PASS" if self.passed else "FAIL")]
        if self.regression_level is not None:
            parts.append("severity " + self.regression_level)
        if self.fingerprint_match is not None:
            parts.append(
                "fingerprint " + ("match" if self.fingerprint_match else "differs")
            )
        if self.removed_steps:
            parts.append("removed: " + ", ".join(self.removed_steps))
        if self.added_steps:
            parts.append("added: " + ", ".join(self.added_steps))
        text = "; ".join(parts)
        if self.reasoning:
            text += ". " + self.reasoning
        return _truncate(text)

    def to_attributes(self) -> Dict[str, AttributeValue]:
        """Flatten into OpenTelemetry attributes under the ``dpk.*`` namespace.

        Every value is an OTel-legal primitive (``str``/``bool``/``int``/``float``) or a
        homogeneous ``list[str]``; ``None`` is never emitted. Keys are listed in
        ``conformance/OTEL_GENAI_MAPPING_v1.md`` §3.
        """
        attrs: Dict[str, AttributeValue] = {
            "dpk.schema": VERDICT_SCHEMA,
            "dpk.sdk.name": _SDK_NAME,
            "dpk.sdk.version": _sdk_version(),
            "dpk.gate.passed": self.passed,
            "dpk.gate.verdict": self.label,
        }
        optional: Sequence[Tuple[str, Any]] = (
            ("dpk.gate.regression_level", self.regression_level),
            ("dpk.gate.strength", self.strength),
            ("dpk.gate.fingerprint_match", self.fingerprint_match),
            ("dpk.gate.policy.max_regression_level", self.max_regression_level),
            ("dpk.gate.policy.allow_divergent_steps", self.allow_divergent_steps),
            ("dpk.gate.policy.profile", self.profile),
            ("dpk.golden.run_id", self.golden_run_id),
            ("dpk.golden.fingerprint", self.golden_fingerprint),
            ("dpk.candidate.run_id", self.candidate_run_id),
            ("dpk.candidate.fingerprint", self.candidate_fingerprint),
        )
        for key, value in optional:
            if value is not None:
                attrs[key] = value
        for key, steps in (
            ("dpk.gate.steps.removed", self.removed_steps),
            ("dpk.gate.steps.added", self.added_steps),
            ("dpk.gate.steps.divergent", self.divergent_steps),
        ):
            if steps:
                attrs[key] = list(steps[:MAX_LIST_ITEMS])
        if self.divergent_steps:
            attrs["dpk.gate.steps.divergent_count"] = len(self.divergent_steps)
        if self.reasoning:
            attrs["dpk.gate.reasoning"] = _truncate(self.reasoning)
        for key, value in self.evidence:
            attrs["dpk.evidence." + key] = value
        return attrs

    def evaluation_result_attributes(
        self, name: str = EVALUATION_NAME
    ) -> Dict[str, AttributeValue]:
        """Attributes for the standard OTel GenAI ``gen_ai.evaluation.result`` event.

        Backends that understand the GenAI semantic conventions but not ``dpk.*``
        (evaluation views keyed on ``gen_ai.evaluation.*``) still see the verdict as a
        binary evaluation: score ``1.0``/``0.0`` with label ``pass``/``fail``, which is
        the binary convention the GenAI ``score.label`` examples use.
        """
        return {
            "gen_ai.evaluation.name": name,
            "gen_ai.evaluation.score.value": self.score,
            "gen_ai.evaluation.score.label": self.label,
            "gen_ai.evaluation.explanation": self.summary(),
        }

    def to_dict(self) -> Dict[str, Any]:
        """A JSON-ready dict (lists, not tuples; evidence as an object)."""
        return {
            "passed": self.passed,
            "verdict": self.label,
            "regression_level": self.regression_level,
            "strength": self.strength,
            "fingerprint_match": self.fingerprint_match,
            "golden_fingerprint": self.golden_fingerprint,
            "candidate_fingerprint": self.candidate_fingerprint,
            "golden_run_id": self.golden_run_id,
            "candidate_run_id": self.candidate_run_id,
            "max_regression_level": self.max_regression_level,
            "allow_divergent_steps": self.allow_divergent_steps,
            "profile": self.profile,
            "removed_steps": list(self.removed_steps),
            "added_steps": list(self.added_steps),
            "divergent_steps": list(self.divergent_steps),
            "reasoning": self.reasoning,
            "evidence": dict(self.evidence),
        }


def _enum_value(value: Any) -> Optional[str]:
    if value is None:
        return None
    return str(getattr(value, "value", value))


def _optional_str(value: Any) -> Optional[str]:
    return None if value is None else str(value)


def _optional_bool(value: Any) -> Optional[bool]:
    return None if value is None else bool(value)


_EVIDENCE_KEY_CHARS = frozenset(string.ascii_letters + string.digits + "_.")


def _evidence_items(evidence: Optional[Mapping[str, str]]) -> Tuple[Tuple[str, str], ...]:
    if not evidence:
        return ()
    items = []
    for key, value in sorted(evidence.items()):
        key = str(key)
        # ASCII only ([A-Za-z0-9_.]+), the published mapping contract; str.isalnum()
        # alone would accept some non-ASCII letters and digits.
        if not key or not all(c in _EVIDENCE_KEY_CHARS for c in key):
            raise ValueError(
                f"evidence key {key!r} must be non-empty and use only ASCII letters, "
                "digits, '_' or '.'"
            )
        items.append((key, str(value)))
    return tuple(items)


__all__ = ["EVALUATION_NAME", "GateVerdict", "VERDICT_SCHEMA", "MAX_TEXT_LENGTH", "MAX_LIST_ITEMS"]

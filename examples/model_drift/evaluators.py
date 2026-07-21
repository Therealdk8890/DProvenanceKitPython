"""Drift evaluators — how similar is "still the same answer"?

A :class:`DriftEvaluator` scores two answers (old model's, new model's) in ``[0, 1]``:
``1.0`` = equivalent, lower = more drift. YOU own this notion of equivalence.

    • :class:`LexicalSimilarityEvaluator` — stdlib-only surface similarity via
      :func:`difflib.SequenceMatcher`. No dependencies, deterministic, good default.

    • :class:`LLMJudgeEvaluator` — asks a model to rate equivalence. It delegates to a
      :class:`~examples.model_drift.providers.ModelClient`, so the vendor SDK is imported
      lazily by that client only when you actually construct a live provider.

:func:`as_equivalence_evaluator` adapts any :class:`DriftEvaluator` into the kit's
:class:`~dprovenancekit.AnyEquivalenceEvaluator`, keyed on the ``generation`` events so the
gate compares the two runs' response payloads with your evaluator.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import TYPE_CHECKING, Any

try:  # Python 3.8+: typing.Protocol
    from typing import Protocol
except ImportError:  # pragma: no cover - Protocol always present on supported versions
    from typing_extensions import Protocol  # type: ignore[assignment]

from dprovenancekit import AnyEquivalenceEvaluator

if TYPE_CHECKING:  # avoid importing providers at runtime (keeps the module graph acyclic)
    from .providers import ModelClient


class DriftEvaluator(Protocol):
    """Scores the equivalence of two answers in ``[0, 1]`` (1.0 = equivalent)."""

    identifier: str

    def score(self, old_response: str, new_response: str) -> float:
        """Return how equivalent ``new_response`` is to ``old_response`` (1.0 = same)."""
        ...


def _clamp01(value: float) -> float:
    # NaN is treated as drift (0.0), never a silent pass.
    if value != value:  # NaN
        return 0.0
    return 0.0 if value < 0.0 else 1.0 if value > 1.0 else value


# ── Lexical (stdlib only) ────────────────────────────────────────────────────────


class LexicalSimilarityEvaluator:
    """Surface-form similarity via :func:`difflib.SequenceMatcher` (stdlib only).

    ``ratio()`` is ``1.0`` for identical text and falls toward ``0.0`` as the two strings
    diverge. Cheap and dependency-free — a sensible default, though it judges *wording*,
    not *meaning* (use :class:`LLMJudgeEvaluator` when a reworded-but-equivalent answer
    should still count as equivalent).
    """

    identifier = "lexical_seqmatch_v1"

    def score(self, old_response: str, new_response: str) -> float:
        return SequenceMatcher(None, old_response, new_response).ratio()


# ── LLM-as-judge (delegates to a ModelClient) ────────────────────────────────────


_JUDGE_INSTRUCTIONS = (
    "You are grading whether two answers to the same question are EQUIVALENT in meaning. "
    "Reply with a single number between 0 and 1: 1.0 = fully equivalent, 0.0 = unrelated "
    "or contradictory. Reply with ONLY the number.\n\n"
    "ANSWER A:\n{old}\n\nANSWER B:\n{new}\n\nEquivalence score:"
)

_NUMBER_RE = re.compile(r"[-+]?\d*\.?\d+")


def _parse_score(raw: str) -> float:
    """Extract a valid ``[0, 1]`` equivalence score from a judge reply.

    Fails CLOSED. Anything we cannot read as a single in-range score — an error string, an
    out-of-range number (``"Error 503"``, ``"9 out of 10"``), or prose with no ``[0, 1]``
    value — scores ``0.0`` (treated as drift) rather than silently passing the check. A
    number outside ``[0, 1]`` is NOT clamped up to a pass; it is discarded as "not a score".
    When several in-range numbers appear (an off-spec reply), the lowest is used —
    conservative toward flagging drift.
    """
    in_range = [
        value
        for value in (float(m.group()) for m in _NUMBER_RE.finditer(raw or ""))
        if 0.0 <= value <= 1.0
    ]
    return min(in_range) if in_range else 0.0


class LLMJudgeEvaluator:
    """Ask a model to rate the equivalence of two answers.

    Intended for LIVE providers: pass a real :class:`ModelClient` and the model to judge
    with. With the offline :class:`FakeModelClient` the reply is not a number, so the score
    falls back to ``0.0`` — use the lexical evaluator for offline demos.
    """

    def __init__(self, model_client: "ModelClient", judge_model: str) -> None:
        self._client = model_client
        self._judge_model = judge_model
        self.identifier = f"llm_judge:{judge_model}"

    def score(self, old_response: str, new_response: str) -> float:
        prompt = _JUDGE_INSTRUCTIONS.format(old=old_response, new=new_response)
        return _parse_score(self._client.complete(self._judge_model, prompt))


# ── Adapter into the kit's equivalence evaluator ─────────────────────────────────


def as_equivalence_evaluator(evaluator: DriftEvaluator) -> AnyEquivalenceEvaluator:
    """Adapt a :class:`DriftEvaluator` into the kit's :class:`AnyEquivalenceEvaluator`.

    The kit hands the callback the two event PAYLOADS (the ``generation`` events). We
    compare the same prompt's answers only — if two payloads carry different ``prompt_id``
    they are not the same step, so they score ``0.0`` (never equivalent).
    """

    def _score(old_payload: Any, new_payload: Any) -> float:
        if getattr(old_payload, "prompt_id", None) != getattr(new_payload, "prompt_id", None):
            return 0.0
        old_response = getattr(old_payload, "response", "")
        new_response = getattr(new_payload, "response", "")
        return _clamp01(float(evaluator.score(old_response, new_response)))

    return AnyEquivalenceEvaluator(
        evaluator_identifier=evaluator.identifier,
        evaluator=_score,
    )

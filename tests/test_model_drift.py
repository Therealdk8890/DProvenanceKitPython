"""Deterministic tests for the ``examples/model_drift`` package.

Every test runs offline with the package's :class:`FakeModelClient` — no network, no API
key, no randomness — so they are safe on every PR. They pin the end-to-end story:

    * parity  → the gate PASSES and reports no drift,
    * drift   → the gate FAILS and names exactly the prompts that drifted,
    * the example imports and runs with ``openai`` NOT installed (the lazy-import contract),
    * :class:`LexicalSimilarityEvaluator` scores 1.0 for identical text and lower — and
      monotonically — as the text diverges.
"""

from __future__ import annotations

import importlib
import os
import sys

import pytest

# Make the ``examples`` namespace package importable when the suite is run with a bare
# ``pytest`` (``python -m pytest`` already puts the repo root on sys.path).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from examples.model_drift.evaluators import LexicalSimilarityEvaluator  # noqa: E402
from examples.model_drift.harness import Prompt, run_drift_check  # noqa: E402
from examples.model_drift.providers import FakeModelClient  # noqa: E402

# A tiny fixed prompt set. ``drifts_on`` keys on the prompt TEXT (what the client receives),
# so we can make exactly one prompt drift and assert on it.
_PROMPTS = [
    Prompt("capital", "What is the capital of France?"),
    Prompt("meters", "How many meters are in a kilometer?"),
    Prompt("focus", "Give one tip for staying focused."),
]


def _drifts_on_focus(text: str) -> bool:
    return "focused" in text.lower()


# ── parity → PASS, no drift ──────────────────────────────────────────────────────


def test_parity_passes_with_no_drift():
    result = run_drift_check(
        old_model="old-model",
        new_model="new-model",
        prompts=_PROMPTS,
        client=FakeModelClient(mode="parity"),
        evaluator=LexicalSimilarityEvaluator(),
        threshold=0.8,
    )

    assert result.passed is True
    assert result.report.passed is True
    assert result.drifted_prompts == []
    # In parity mode every model returns the same text, so every score is a perfect 1.0.
    assert all(entry.score == 1.0 and not entry.drifted for entry in result.per_prompt)


# ── drift → FAIL, names the drifted prompts ──────────────────────────────────────


def test_drift_fails_and_reports_drifted_prompts():
    result = run_drift_check(
        old_model="old-model",
        new_model="new-model",
        prompts=_PROMPTS,
        client=FakeModelClient(mode="drift", drifts_on=_drifts_on_focus),
        evaluator=LexicalSimilarityEvaluator(),
        threshold=0.8,
    )

    assert result.passed is False
    assert result.report.passed is False
    # The kit's gate escalates a changed CRITICAL step to a HIGH regression.
    assert result.report.regression_level.value == "high"
    # Only the one drifting prompt is flagged; the parity prompts stay clean.
    assert result.drifted_prompts == ["focus"]
    by_id = {entry.prompt_id: entry for entry in result.per_prompt}
    assert by_id["focus"].drifted and by_id["focus"].score < 0.8
    assert by_id["capital"].score == 1.0 and not by_id["capital"].drifted
    assert by_id["meters"].score == 1.0 and not by_id["meters"].drifted


def test_cli_exit_codes_mirror_the_gate():
    # The CLI returns a shell exit code: 0 on parity, 1 on drift (like ``dpk gate``).
    from examples.model_drift.__main__ import main

    parity_code = main(["--old", "old-model", "--new", "new-model", "--fake", "parity"])
    drift_code = main(["--old", "old-model", "--new", "new-model", "--fake", "drift"])

    assert parity_code == 0
    assert drift_code == 1


# ── imports + runs with openai NOT installed (the lazy-import contract) ───────────


def test_example_imports_and_runs_without_openai(monkeypatch):
    # Simulate ``openai`` being absent: setting the entry to None makes ``import openai``
    # raise ImportError, regardless of whether the SDK is installed in this environment.
    monkeypatch.setitem(sys.modules, "openai", None)
    monkeypatch.delitem(sys.modules, "examples.model_drift.providers", raising=False)

    # The module imports fine with openai absent — nothing imports it at module top.
    providers = importlib.import_module("examples.model_drift.providers")

    # The deterministic fake path runs with no SDK and no key.
    client = providers.FakeModelClient(mode="parity")
    assert client.complete("any-model", "hello?") == client.complete("other-model", "hello?")

    # Constructing the live client is the only thing that needs the SDK, and it fails with a
    # clear, actionable error rather than an import-time crash.
    with pytest.raises(ImportError, match="openai"):
        providers.OpenAIModelClient()


# ── LexicalSimilarityEvaluator: 1.0 for identical, lower + monotonic as it diverges ──


def test_lexical_similarity_is_one_for_identical_and_monotonic_on_divergence():
    evaluator = LexicalSimilarityEvaluator()
    base = "The refund window for digital goods is thirty days."

    identical = evaluator.score(base, base)
    minor = evaluator.score(base, "The refund window for digital goods is thirty-one days.")
    major = evaluator.score(base, "The refund window for digital goods is fourteen days total.")
    unrelated = evaluator.score(base, "Photosynthesis converts sunlight into chemical energy.")

    # Identical text is a perfect match.
    assert identical == 1.0
    # Every score is a valid similarity in [0, 1].
    for value in (identical, minor, major, unrelated):
        assert 0.0 <= value <= 1.0
    # More divergence => a lower score (monotonic-ish).
    assert identical > minor > major > unrelated


# ── LLM judge fails CLOSED on unparseable / out-of-range replies (regression) ──────


@pytest.mark.parametrize(
    "reply, expected",
    [
        ("1.0", 1.0),
        ("0.85", 0.85),
        ("0", 0.0),
        (" 0.5 ", 0.5),
        ("Error 503: service unavailable", 0.0),
        ("HTTP 429 Too Many Requests", 0.0),
        ("I'd rate these a 9 out of 10", 0.0),
        ("5 days vs 30 days, not equivalent", 0.0),
        ("", 0.0),
        ("totally different", 0.0),
    ],
)
def test_llm_judge_parse_fails_closed(reply, expected):
    # A judge reply we can't read as a single [0, 1] score must score 0.0 (drift), never
    # get clamped up into a silent pass. Regression test for the fail-open parser bug.
    from examples.model_drift.evaluators import _parse_score

    assert _parse_score(reply) == expected


def test_llm_judge_error_string_reads_as_drift():
    # A provider that returns an ERROR STRING (not a number) must fail closed to drift, so a
    # broken judge can't silently pass a real regression.
    from examples.model_drift.evaluators import LLMJudgeEvaluator

    class _ErrorClient:
        def complete(self, model: str, prompt: str) -> str:
            return "Error 503: service unavailable"

    judge = LLMJudgeEvaluator(_ErrorClient(), judge_model="judge-x")
    assert judge.score("5 days", "30 days") == 0.0


def test_clamp01_treats_nan_as_drift():
    # A custom evaluator returning NaN must read as drift, not slip through the clamp.
    from examples.model_drift.evaluators import _clamp01

    assert _clamp01(float("nan")) == 0.0


# ── Saved golden baseline: capture once, gate later without the old model ──────────


def test_saved_golden_is_reused_without_calling_the_old_model(tmp_path):
    from examples.model_drift.harness import record_baseline

    golden_db = str(tmp_path / "golden.sqlite")

    # 1. Capture the OLD model's baseline while it is still callable.
    record_baseline(
        model="old-model",
        prompts=_PROMPTS,
        client=FakeModelClient(mode="parity"),
        db_path=golden_db,
    )

    # 2. A client that answers the NEW model but blows up if the retired OLD model is called
    #    — proving the gate reuses the saved golden instead of re-recording it.
    class _RetiredOldModelClient:
        def __init__(self) -> None:
            self._fake = FakeModelClient(mode="parity")

        def complete(self, model: str, prompt: str) -> str:
            if model == "old-model":
                raise AssertionError("the retired old model must not be called")
            return self._fake.complete(model, prompt)

    result = run_drift_check(
        old_model="old-model",
        new_model="new-model",
        prompts=_PROMPTS,
        client=_RetiredOldModelClient(),
        evaluator=LexicalSimilarityEvaluator(),
        threshold=0.8,
        golden_db=golden_db,
    )

    # Parity answers depend only on the prompt, so the reused golden and the fresh candidate
    # match: no drift.
    assert result.passed is True
    assert result.drifted_prompts == []


def test_run_drift_check_errors_when_golden_db_has_no_baseline(tmp_path):
    from dprovenancekit import SQLiteTraceStore

    from examples.model_drift.harness import GenerationEvent

    empty_db = str(tmp_path / "empty.sqlite")
    with SQLiteTraceStore(GenerationEvent, empty_db, start_writer=False):
        pass  # a store file with no runs recorded

    with pytest.raises(ValueError, match="no golden run"):
        run_drift_check(
            old_model="old-model",
            new_model="new-model",
            prompts=_PROMPTS,
            client=FakeModelClient(mode="parity"),
            evaluator=LexicalSimilarityEvaluator(),
            threshold=0.8,
            golden_db=empty_db,
        )


def test_cli_record_golden_then_gate_reuses_it(tmp_path):
    from examples.model_drift.__main__ import main

    golden_db = str(tmp_path / "golden.sqlite")

    assert (
        main(
            ["--old", "old-model", "--record-golden", "--golden-db", golden_db, "--fake", "parity"]
        )
        == 0
    )
    # Gate a candidate against the saved golden — parity → exit 0.
    assert (
        main(
            ["--old", "old-model", "--new", "new-model", "--golden-db", golden_db, "--fake", "parity"]
        )
        == 0
    )


def test_cli_usage_errors_return_2():
    from examples.model_drift.__main__ import main

    # --record-golden with nowhere to write the baseline.
    assert main(["--old", "old-model", "--record-golden", "--fake", "parity"]) == 2
    # A gate run with no --new.
    assert main(["--old", "old-model", "--fake", "parity"]) == 2

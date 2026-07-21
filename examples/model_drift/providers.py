"""Model providers — the thing that turns a prompt into an answer.

A :class:`ModelClient` is any object with ``complete(model, prompt) -> str``. The kit never
calls a model for you; you own the provider (and its API key).

Two clients ship here:

    • :class:`FakeModelClient` — deterministic, offline. Derives each answer from the
      prompt text via a pure rule (no network, no randomness), so the whole drift flow
      runs with zero setup. It can simulate PARITY (both models answer identically) or
      DRIFT (the newer model diverges on a subset of prompts) for demos, tests and CI.

    • :class:`OpenAIModelClient` — a live client. It imports ``openai`` LAZILY, inside
      ``__init__``, so *this module imports fine even when ``openai`` is not installed*.
      Install it with ``pip install "dprovenancekit[openai]"`` to run a live check.
"""

from __future__ import annotations

import hashlib
from typing import Callable, List, Optional

try:  # Python 3.8+: typing.Protocol
    from typing import Protocol
except ImportError:  # pragma: no cover - Protocol always present on supported versions
    from typing_extensions import Protocol  # type: ignore[assignment]


class ModelClient(Protocol):
    """Anything that can answer a prompt with a given model.

    Implement this to plug in your own provider (Anthropic, a local model, an internal
    gateway, …). ``complete`` must be deterministic enough to baseline — set temperature
    to 0 for live providers so a re-run of the golden reproduces it.
    """

    def complete(self, model: str, prompt: str) -> str:
        """Return the model's answer to ``prompt``."""
        ...


# ── Deterministic offline client (demos / tests / CI) ────────────────────────────


def _default_drift_selector(prompt: str) -> bool:
    """Which prompts drift in ``drift`` mode: a stable, content-derived ~half of them."""
    digest = hashlib.sha1(prompt.strip().lower().encode("utf-8")).hexdigest()
    return int(digest, 16) % 2 == 0


def _topic(prompt: str) -> str:
    words = prompt.strip().rstrip("?.!").split()
    return " ".join(words[:4]) if words else "the question"


# A small fixed vocabulary the drift answers are drawn from. Two different model names hash
# to two different word sequences, so their answers diverge substantially (low similarity).
_LEXICON = (
    "policy update depends context reverse affirm deny caveat revised current legacy "
    "nuance threshold escalate default guidance evidence tradeoff baseline divergent "
    "clarify restrict permit outdated"
).split()


class FakeModelClient:
    """A deterministic, offline stand-in for a real model.

    The answer is a pure function of the prompt (and, on the drifting subset in ``drift``
    mode, of the model name), so runs are perfectly reproducible.

    Modes:
        * ``"parity"`` — the answer depends ONLY on the prompt, so every model returns the
          same text: a replacement can never drift.
        * ``"drift"``  — on the prompts selected by ``drifts_on`` the answer also depends on
          the model name, so two different models produce different text (drift); all other
          prompts stay in parity.

    Pass ``drifts_on`` to control exactly which prompts drift (handy in tests); it defaults
    to a stable hash of the prompt that drifts roughly half of any prompt set.
    """

    def __init__(
        self,
        *,
        mode: str = "parity",
        drifts_on: Optional[Callable[[str], bool]] = None,
    ) -> None:
        if mode not in ("parity", "drift"):
            raise ValueError(f"mode must be 'parity' or 'drift', got {mode!r}")
        self.mode = mode
        self._drifts_on = drifts_on or _default_drift_selector

    def complete(self, model: str, prompt: str) -> str:
        if self.mode == "drift" and self._drifts_on(prompt):
            return self._drifted_answer(model, prompt)
        return self._base_answer(prompt)

    @staticmethod
    def _base_answer(prompt: str) -> str:
        topic = _topic(prompt)
        return (
            f"Regarding {topic}: the established answer is that the standard, well-sourced "
            f"guidance applies, with the usual caveats noted."
        )

    @staticmethod
    def _drifted_answer(model: str, prompt: str) -> str:
        # A model-derived word sequence, so two different models produce genuinely
        # different answers to the same prompt (not just a different name tag). The digest
        # of (model, prompt) picks distinct words from the lexicon; distinct model names
        # give distinct digests and therefore substantially different text.
        topic = _topic(prompt)
        digest = hashlib.sha256(f"{model}::{prompt}".encode("utf-8")).digest()
        picks: List[str] = []
        seen = set()
        for byte in digest:
            word = _LEXICON[byte % len(_LEXICON)]
            if word not in seen:
                seen.add(word)
                picks.append(word)
            if len(picks) >= 12:
                break
        return f"On {topic} the position is now: " + ", ".join(picks) + "."


# ── Live client (opt-in; lazy vendor import) ─────────────────────────────────────


class OpenAIModelClient:
    """A live :class:`ModelClient` backed by the OpenAI Python SDK.

    The ``openai`` import happens INSIDE ``__init__`` (never at module top), so importing
    this module — and using :class:`FakeModelClient` — works with ``openai`` absent and no
    API key. Constructing this client is what requires the dependency.

    The key is read from the ``OPENAI_API_KEY`` environment variable unless you pass one
    explicitly. Temperature defaults to 0 so a golden baseline is reproducible.
    """

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        temperature: float = 0.0,
    ) -> None:
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - exercised only without openai
            raise ImportError(
                "OpenAIModelClient requires the 'openai' package. Install it with:\n"
                '    pip install "dprovenancekit[openai]"'
            ) from exc

        self._temperature = temperature
        self._client = OpenAI(api_key=api_key, base_url=base_url)

    def complete(self, model: str, prompt: str) -> str:
        response = self._client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=self._temperature,
        )
        return response.choices[0].message.content or ""

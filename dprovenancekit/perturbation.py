"""A diagnostic layer that perturbs the equivalence evaluator for stability testing.

Gated by the :class:`~dprovenancekit.benchmark.DeterministicBoundary`: when the boundary
declares the environment cache-isolated, the base evaluator is returned unchanged (fully
deterministic). Only when isolation is lifted does score noise flow in — which can flip
pairs across the matching threshold and so move measured precision/recall/F1.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from .alignment_config import AnyEquivalenceEvaluator


@dataclass(frozen=True)
class PerturbationMode:
    """``none`` is a passthrough; ``score_noise`` shifts scores by +/- ``amplitude``."""

    kind: str  # "none" | "scoreNoise"
    amplitude: float = 0.0

    @staticmethod
    def none() -> "PerturbationMode":
        return PerturbationMode("none")

    @staticmethod
    def score_noise(amplitude: float) -> "PerturbationMode":
        return PerturbationMode("scoreNoise", amplitude)


class EvaluationPerturbationLayer:
    def __init__(self, mode: PerturbationMode):
        self.mode = mode

    def evaluator(
        self, base: AnyEquivalenceEvaluator, boundary
    ) -> AnyEquivalenceEvaluator:
        if (
            self.mode.kind != "scoreNoise"
            or boundary.cache_isolated
            or self.mode.amplitude <= 0
        ):
            # Isolated (or no perturbation): deterministic passthrough.
            return base

        amplitude = self.mode.amplitude

        def noisy(a, b):
            s = base.evaluate_similarity(a, b)
            noise = random.uniform(-amplitude, amplitude)
            return max(0.0, min(1.0, s + noise))

        return AnyEquivalenceEvaluator(
            evaluator_identifier=base.evaluator_identifier + "+noise",
            evaluator=noisy,
            ambiguity_threshold_fn=lambda e: base.ambiguity_threshold(e),
        )

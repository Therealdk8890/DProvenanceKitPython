"""The drift-check harness — record two runs, gate the candidate against the golden.

:func:`run_drift_check` is the whole story in one call:

    1. Record the OLD model's answers as a GOLDEN run and the NEW model's answers as a
       CANDIDATE run — one CRITICAL ``generation`` event per prompt, carrying
       ``{prompt_id, model, response}``, in the same prompt order — into a SQLite store.
    2. Gate the candidate against the golden with the kit's :class:`RegressionGate`, using
       your :class:`DriftEvaluator` to decide when two answers are equivalent. A per-prompt
       answer below the threshold surfaces as a CRITICAL regression (= drift) and fails.
    3. Return a structured :class:`DriftCheckResult`: overall pass/fail, a per-prompt score,
       and exactly which prompts drifted.

Only the PUBLIC DProvenanceKit API is used.
"""

from __future__ import annotations

import os
import tempfile
import uuid
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, cast

from dprovenancekit import (
    AlignmentMode,
    AlignmentProfile,
    AlignmentStrategy,
    DProvenanceKit,
    RegressionGate,
    RegressionReport,
    SQLiteTraceStore,
    TraceableEvent,
    TracePriority,
    TraceRun,
)

from .evaluators import DriftEvaluator, as_equivalence_evaluator
from .providers import ModelClient

DEFAULT_THRESHOLD = 0.8


# ── The recorded event: one generation per prompt ────────────────────────────────


@dataclass(frozen=True)
class GenerationEvent(TraceableEvent):
    """One model answer to one prompt — the unit the drift check records and gates."""

    prompt_id: str
    model: str
    response: str

    @property
    def type_identifier(self) -> str:
        # Fold the prompt id into the type so the aligner binds each prompt 1:1 across the
        # golden and candidate runs, instead of greedily matching by response text.
        return f"generation:{self.prompt_id}"

    @property
    def priority(self) -> TracePriority:
        # CRITICAL so a changed answer escalates to a real regression: the engine only
        # raises severity on CRITICAL steps, and that HIGH severity is what "drift" means.
        return TracePriority.CRITICAL

    def to_dict(self) -> dict:
        return {"prompt_id": self.prompt_id, "model": self.model, "response": self.response}

    @classmethod
    def from_dict(cls, data: dict) -> "GenerationEvent":
        return cls(
            prompt_id=data["prompt_id"],
            model=data.get("model", ""),
            response=data.get("response", ""),
        )


# ── Inputs and results ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Prompt:
    """A fixed evaluation prompt. ``id`` keys it across the two runs; ``text`` is asked."""

    id: str
    text: str


@dataclass(frozen=True)
class PromptDrift:
    """The verdict for one prompt: the two answers and their equivalence score."""

    prompt_id: str
    prompt: str
    old_response: str
    new_response: str
    score: float
    drifted: bool


@dataclass(frozen=True)
class DriftCheckResult:
    """The outcome of a drift check. ``passed`` is the gate verdict (True = no drift)."""

    passed: bool
    threshold: float
    old_model: str
    new_model: str
    per_prompt: List[PromptDrift]
    report: RegressionReport  # the kit's RegressionGate report, for the full diagnostic

    @property
    def drifted_prompts(self) -> List[str]:
        return [p.prompt_id for p in self.per_prompt if p.drifted]


# ── Internals ────────────────────────────────────────────────────────────────────


def _clamp01(value: float) -> float:
    # NaN is treated as drift (0.0) so a custom evaluator's NaN can never read as a pass.
    if value != value:  # NaN
        return 0.0
    return 0.0 if value < 0.0 else 1.0 if value > 1.0 else value


def _drift_profile(threshold: float) -> AlignmentProfile:
    """A strict-audit-shaped profile whose EFFECTIVE response-similarity cutoff is
    ``threshold``.

    ``semantic_threshold`` is compared to the COMBINED weighted score, not the raw
    evaluator output. Under strict-audit weights (type 0.5 / payload 0.5) with a matching
    ``type_identifier``, combined = ``0.5 + 0.5 * response_similarity``. To make the answer
    cutoff equal ``threshold`` we set ``semantic_threshold = 0.5 + 0.5 * threshold``: an
    answer scoring below ``threshold`` lands below it and is flagged as drift.
    """
    return AlignmentProfile(
        strategy=AlignmentStrategy.STRICT_AUDIT,
        version=1,
        type_weight=0.5,
        payload_weight=0.5,
        structural_weight=0.0,
        temporal_weight=0.0,
        semantic_threshold=0.5 + 0.5 * threshold,
        max_ambiguous_candidates=1,
        ambiguity_delta_threshold=0.0,
        alignment_mode=AlignmentMode.LINEAR,
    )


def _record_model_run(
    kit: DProvenanceKit,
    store: SQLiteTraceStore,
    model: str,
    prompts: Sequence[Prompt],
    client: ModelClient,
) -> uuid.UUID:
    """Ask ``model`` every prompt and record one CRITICAL generation event per answer."""
    with kit.run(context_id=model, store=store) as run:
        # Fixed engine name for both runs: the only thing that differs golden-vs-candidate
        # is the answer text, so the gate isolates response drift.
        with kit.with_engine("llm"):
            for prompt in prompts:
                response = client.complete(model, prompt.text)
                kit.record(
                    GenerationEvent(prompt_id=prompt.id, model=model, response=response)
                )
        return run.run_id


def _responses_by_prompt(run: TraceRun) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for event in run.events:
        payload = cast(GenerationEvent, event.payload)
        out[payload.prompt_id] = payload.response
    return out


def _latest_run_by_context(store: SQLiteTraceStore, context_id: str) -> Optional[TraceRun]:
    """Return the newest recorded run with this ``context_id`` (or ``None``).

    ``list_run_metadata`` yields rows newest-first, so the first context match is the newest.
    """
    for row in store.list_run_metadata():
        if row.context_id == context_id:
            return store.get_run(uuid.UUID(row.run_id))
    return None


# ── The public calls ─────────────────────────────────────────────────────────────


def record_baseline(
    *,
    model: str,
    prompts: Sequence[Prompt],
    client: ModelClient,
    db_path: str,
) -> uuid.UUID:
    """Record ONLY ``model``'s answers as a golden run into ``db_path``.

    Run this while the outgoing model is still callable. The db becomes the durable baseline
    you gate future candidates against via ``run_drift_check(..., golden_db=db_path)`` — so
    the check keeps working after the old model is retired. Returns the golden run id.
    """
    with SQLiteTraceStore(GenerationEvent, db_path, start_writer=False) as store:
        kit = DProvenanceKit(GenerationEvent)
        return _record_model_run(kit, store, model, prompts, client)


def run_drift_check(
    *,
    old_model: str,
    new_model: str,
    prompts: Sequence[Prompt],
    client: ModelClient,
    evaluator: DriftEvaluator,
    threshold: float = DEFAULT_THRESHOLD,
    db_path: Optional[str] = None,
    golden_db: Optional[str] = None,
) -> DriftCheckResult:
    """Record both models on ``prompts`` and gate the new model for drift.

    Args:
        old_model: the soon-to-be-discontinued model (its answers are the GOLDEN baseline).
        new_model: the replacement model (the CANDIDATE).
        prompts: the fixed prompt set — you own it; the check is only as good as it is.
        client: your :class:`ModelClient` (offline fake, OpenAI, or your own provider).
        evaluator: your :class:`DriftEvaluator` — what "still the same answer" means.
        threshold: minimum per-prompt equivalence in ``[0, 1]`` to count as no-drift.
        db_path: where to record the runs. ``None`` uses a throwaway temp db.
        golden_db: reuse a golden baseline recorded earlier with :func:`record_baseline`
            (keyed on ``old_model``) instead of calling the old model — this is what lets you
            gate after the old model is retired. ``None`` records the old model now.

    Returns:
        A :class:`DriftCheckResult` with the pass/fail verdict, per-prompt scores, and the
        list of drifted prompt ids.
    """
    threshold = _clamp01(threshold)

    # The golden (old model). Reuse a saved baseline when given — that is what lets you gate
    # after the old model is retired; otherwise record it now, beside the candidate.
    golden: Optional[TraceRun] = None
    if golden_db is not None:
        with SQLiteTraceStore(GenerationEvent, golden_db, start_writer=False) as gstore:
            golden = _latest_run_by_context(gstore, old_model)
        if golden is None:
            raise ValueError(
                f"no golden run for model '{old_model}' in {golden_db}; record one with "
                "record_baseline(...) while the old model is still callable"
            )

    created_temp = db_path is None
    if db_path is None:
        handle, db_path = tempfile.mkstemp(prefix="model_drift_", suffix=".sqlite")
        os.close(handle)

    try:
        # Record the candidate (and the golden too, unless a saved one was reused) into a
        # trace store. get_run flushes, then fetches a detached TraceRun.
        with SQLiteTraceStore(GenerationEvent, db_path, start_writer=False) as store:
            kit = DProvenanceKit(GenerationEvent)
            if golden is None:
                golden = store.get_run(
                    _record_model_run(kit, store, old_model, prompts, client)
                )
            candidate = store.get_run(
                _record_model_run(kit, store, new_model, prompts, client)
            )

        if golden is None or candidate is None:  # pragma: no cover - defensive
            raise RuntimeError("failed to read back the recorded runs from the trace store")

        # 2. Gate the candidate against the golden with our answer-equivalence evaluator.
        #    A per-prompt answer below `threshold` falls below the profile's semantic
        #    threshold, flags that CRITICAL step as changed-beyond-equivalence, and fails.
        gate = RegressionGate(
            profile=_drift_profile(threshold),
            evaluator=as_equivalence_evaluator(evaluator),
        )
        report = gate.check(golden, candidate)

        # 3. Per-prompt breakdown, read back from the recorded runs.
        old_by_prompt = _responses_by_prompt(golden)
        new_by_prompt = _responses_by_prompt(candidate)
        per_prompt: List[PromptDrift] = []
        for prompt in prompts:
            old_response = old_by_prompt.get(prompt.id, "")
            new_response = new_by_prompt.get(prompt.id, "")
            score = _clamp01(float(evaluator.score(old_response, new_response)))
            per_prompt.append(
                PromptDrift(
                    prompt_id=prompt.id,
                    prompt=prompt.text,
                    old_response=old_response,
                    new_response=new_response,
                    score=score,
                    drifted=score < threshold,
                )
            )

        return DriftCheckResult(
            passed=report.passed,
            threshold=threshold,
            old_model=old_model,
            new_model=new_model,
            per_prompt=per_prompt,
            report=report,
        )
    finally:
        if created_temp:
            # Best-effort cleanup of the throwaway db (and its WAL/SHM sidecars).
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(db_path + suffix)
                except OSError:
                    pass

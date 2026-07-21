# Model-drift check

Did a replacement model **drift** from the one it retires?

You are about to swap a soon-to-be-discontinued model for a newer one. Before you flip the
switch you want a crisp, reproducible answer to one question: on the prompts you actually
care about, does the replacement answer *equivalently* to the outgoing model — or does it
quietly drift?

This example wires that check on top of DProvenanceKit. It records the **old** model's
answers as a *golden* run and the **new** model's answers as a *candidate* run (one
`CRITICAL` `generation` event per prompt, carrying `{prompt_id, model, response}`), then
gates the candidate against the golden with a `RegressionGate`. A per-prompt answer that
falls below *your* equivalence threshold surfaces as a critical regression — i.e. drift —
and the check fails with exit code `1` (parity exits `0`), so it drops straight into CI.

## The honest framing

DProvenanceKit is the **record + gate substrate**. It does not call a model, own your
prompts, or decide what "the same answer" means. **You** own the three things that make the
check meaningful:

- **The prompt set** — what "the answers you care about" actually are. The check is only as
  good as the prompts you pick.
- **The API key / provider access** — the kit never calls a model for you. You bring the
  `ModelClient`.
- **The drift threshold and the equivalence notion** — lexical similarity? an LLM judge?
  your own rule? How similar is "still the same answer" for *your* use case.

## The timing catch — capture the golden before the old model is gone

The golden baseline is the **old model's** answers, so you must capture them **while the old
model is still callable.** Once it is discontinued you can no longer produce the baseline.

By default a single run records *both* models, so it needs both live at once — fine for a
pre-cutover go/no-go. To keep gating **after** the old model is retired, capture the golden
once and reuse it:

```bash
# While the outgoing model is still callable — record ONLY its answers as the golden.
python -m examples.model_drift --old gpt-5.4-2026-03-05 --record-golden \
    --golden-db golden.sqlite --prompts my_prompts.jsonl --live

# Any time later (even after gpt-5.4 is gone) — gate a candidate against the SAVED golden.
# The old model is never called; only the candidate runs.
python -m examples.model_drift --old gpt-5.4-2026-03-05 --new gpt-5.4-pro-2026-03-05 \
    --golden-db golden.sqlite --prompts my_prompts.jsonl --live
```

`record_baseline(...)` and `run_drift_check(..., golden_db=...)` are the Python equivalents.

## Run it (zero setup, offline)

The package ships a deterministic `FakeModelClient` — answers are a pure function of the
prompt (no network, no key, no randomness), so the whole flow runs anywhere. It can
simulate **parity** (both models answer identically) or **drift** (the newer model diverges
on a subset of prompts).

```bash
# Parity — the replacement matches the outgoing model. Exits 0.
python -m examples.model_drift --old old-model --new new-model --fake parity

# Drift — the replacement diverges on some prompts. Prints which ones, exits 1.
python -m examples.model_drift --old old-model --new new-model --fake drift
```

Example drift output:

```
Model drift check: OLD=old-model  NEW=new-model  (judge=lexical, threshold=0.80)
  prompt        score  verdict
  greeting       0.66  DRIFT
  refund         1.00  ok
  ...
Result: DRIFT — 4 of 8 prompts drifted: greeting, password, summary, email
```

## Run it live against OpenAI

Say you are retiring `gpt-5.4` (GPT-5.4 Thinking) for `gpt-5.4-pro` (GPT-5.4 Pro).

**Pin the dated snapshots for the go/no-go.** A drift baseline must be reproducible: baseline
against the floating `gpt-5.4` alias and it can move under you, so you can no longer tell "the
candidate drifted" from "my baseline drifted." The dated snapshot also freezes the exact
behavior you are about to lose.

```bash
pip install "dprovenancekit[openai]"
export OPENAI_API_KEY=sk-...

python -m examples.model_drift \
    --old gpt-5.4-2026-03-05 --new gpt-5.4-pro-2026-03-05 \
    --prompts my_prompts.jsonl \
    --live --judge llm --judge-model gpt-5.4-pro-2026-03-05 \
    --threshold 0.85
```

**Prefer `--judge llm` over the lexical default for a base→pro swap.** A "pro" model tends to
reword and elaborate far more than it changes meaning, so lexical similarity flags a pile of
false drift on wording alone; the LLM judge scores *semantic* equivalence instead. Pick a
neutral, pinned grader with `--judge-model` (ideally not one of the two under test, to avoid a
model favouring its own style). The judge fails **closed**: any reply that is not a clean
`[0, 1]` score — an error string, an out-of-range number — reads as drift, so a flaky judge
can never silently pass a regression. It costs one extra call per prompt.

**Keep watching the floating alias after cutover (drift surveillance).** Once you depend on the
moving `gpt-5.4-pro` alias, OpenAI can update it under you. Gate the saved dated golden against
the *floating* candidate on a schedule to catch that:

```bash
python -m examples.model_drift \
    --old gpt-5.4-2026-03-05 --new gpt-5.4-pro \
    --golden-db golden.sqlite --prompts my_prompts.jsonl \
    --live --judge llm --judge-model gpt-5.4-pro-2026-03-05 --threshold 0.85
```

`--live` uses `OpenAIModelClient`, which imports `openai` **lazily** (only when constructed),
so the module and the whole `--fake` path import fine with `openai` absent. Temperature is 0
(the default) so a re-run of the golden reproduces it.

## Bring your own prompt set

Prompts are a JSONL file — one `{"id": ..., "prompt": ...}` per line
(`prompts.sample.jsonl` is a starter set):

```jsonl
{"id": "refund_policy", "prompt": "What is our refund window for digital goods?"}
{"id": "escalation", "prompt": "When should a support agent escalate to a human?"}
```

```bash
python -m examples.model_drift --old old-model --new new-model \
    --prompts my_prompts.jsonl --fake drift
```

Keep the `id`s stable across runs — they key each prompt 1:1 between the golden and
candidate, so the gate compares like with like.

## Bring your own equivalence notion

A `DriftEvaluator` scores two answers in `[0, 1]` (`1.0` = equivalent). Two ship here:

- `LexicalSimilarityEvaluator` — stdlib-only surface similarity via
  `difflib.SequenceMatcher`. Deterministic, dependency-free, the default. Judges *wording*,
  not *meaning*.
- `LLMJudgeEvaluator(model_client, judge_model)` — asks a model to rate equivalence, so a
  reworded-but-equivalent answer can still count as equivalent.

Write your own by implementing `score(old_response, new_response) -> float` and an
`identifier`, then hand it to `run_drift_check(..., evaluator=your_evaluator)`. The helper
`as_equivalence_evaluator` adapts any `DriftEvaluator` into the kit's
`AnyEquivalenceEvaluator`, keyed on the `generation` events so the gate compares the two
runs' response payloads with your rule.

## From Python

```python
from examples.model_drift.harness import Prompt, run_drift_check
from examples.model_drift.providers import FakeModelClient
from examples.model_drift.evaluators import LexicalSimilarityEvaluator

result = run_drift_check(
    old_model="old-model",
    new_model="new-model",
    prompts=[Prompt("capital", "What is the capital of France?")],
    client=FakeModelClient(mode="drift"),
    evaluator=LexicalSimilarityEvaluator(),
    threshold=0.8,
    db_path="drift.sqlite",   # durable path keeps the golden baseline
)
print(result.passed, result.drifted_prompts)
```

## How the threshold maps onto the gate

`RegressionGate`'s `semantic_threshold` is compared to the *combined* weighted alignment
score, not the raw evaluator output. Under strict-audit weights (type `0.5` / payload `0.5`)
with a matching `type_identifier`, `combined = 0.5 + 0.5 * response_similarity`. The harness
therefore builds a profile with `semantic_threshold = 0.5 + 0.5 * threshold`, so your
`--threshold` *is* the effective response-similarity cutoff: an answer scoring below it lands
below the profile threshold, its `CRITICAL` step is flagged changed-beyond-equivalence, and
the gate fails. See `harness.py::_drift_profile` for the derivation.

## Files

| File | What it is |
| --- | --- |
| `providers.py` | `ModelClient` protocol; deterministic `FakeModelClient`; lazy-import `OpenAIModelClient`. |
| `evaluators.py` | `DriftEvaluator` protocol; `LexicalSimilarityEvaluator`; `LLMJudgeEvaluator`; `as_equivalence_evaluator` adapter. |
| `harness.py` | `run_drift_check(...)` — records both runs and gates the candidate. Returns a `DriftCheckResult`. |
| `__main__.py` | The CLI (`python -m examples.model_drift`). |
| `prompts.sample.jsonl` | A starter prompt set. |

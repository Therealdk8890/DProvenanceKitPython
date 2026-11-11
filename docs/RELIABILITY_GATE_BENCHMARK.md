# Synthetic regression-gate policy sensitivity v1

**Purpose:** make the false-alarm versus missed-regression trade-off visible when
choosing DProvenanceKit's gate policy. This is an initial, reproducible **synthetic**
benchmark. It is **not** evidence of production false-positive rates, statistical
generalization, model reliability, or superiority to other observability tools.

## Run

From a Python SDK checkout with the package installed:

```bash
python examples/reliability_gate_benchmark.py
python examples/reliability_gate_benchmark.py --output reports/gate-policy-v1.json
python examples/reliability_gate_benchmark.py --fail-on-missed-regression
python -m pytest tests/test_reliability_gate_benchmark.py
```

The script uses only the Python SDK and standard library. It runs the **production
RegressionGate**, not a second test-only implementation. Results are deterministic
JSON: scenario labels, gate verdicts, severity, per-policy confusion matrices,
false-positive rates, false-negative rates, and case IDs behind disagreements.

## Policies evaluated

| Policy | Configuration | Trade-off |
| --- | --- | --- |
| Default strict | `RegressionGate()` | Blocks any instrumented structural divergence, including benign additions |
| Severity only | `RegressionGate(allow_divergent_steps=True)` | Allows noncritical divergence but still blocks HIGH-severity critical-path regressions |

Both policies use the default strict-audit profile, exact payload comparison,
minimum priority STRUCTURAL, and maximum accepted severity NONE. **Neither
changes the engine**, lowers the severity threshold, or treats a matching
fingerprint as proof of correctness.

## Ground truth and coverage

Ten manually labeled candidate traces cover five intended benign variations
(identical rerun; changed retrieval payload; added telemetry; optional structural
step; two independent critical checks reordered) and five intended regressions
(missing verification; changed critical decision; inverted required dependency;
missing decision; both critical steps absent).

Benignity is a *domain label*, not something inferred from the gate. In
particular, the independent-critical-reorder scenario deliberately exposes a
potential false alarm: current matched-critical order checking does not infer
whether two steps are causally dependent. If a team requires both checks in a
specific order, it must label that scenario as a regression instead.

Confusion-matrix definitions, with **positive = a labeled regression**:

- TP: regression blocked; FN: regression allowed
- FP: benign change blocked; TN: benign change allowed
- FPR = FP / (FP + TN); FNR = FN / (FN + TP)
- When a denominator is zero, its rate is `null` rather than a fabricated zero.

The test enforces zero **synthetic** missed regressions for these scenarios, not
an unconditional guarantee about arbitrary real runs.

## Limitations and next evidence gate

- Only ten hand-designed traces, with no repeated LLM sampling, production
  instrumentation gaps, tool failures, real framework changes, or labeling
  uncertainty. No confidence interval is meaningful at this scale.
- The script validates policy sensitivity, not predictive accuracy under the
  distribution of real agent workflows. It does not compare with LangSmith,
  Phoenix, or other vendors.
- The next benchmark should ingest **consented, independently labeled pairs of
  recorded traces** from at least two supported frameworks; record the SDK
  version, framework version, instrumentation coverage, gate configuration,
  scenario origin, and repetition count with every observation.
- Report metrics separately by workflow and failure mode, and measure developer
  override rates alongside FPR/FNR. Do not pool unrelated scenarios into a
  headline success rate without showing denominators.
- A regression-gate decision is about the **instrumented path**. It cannot
  establish hidden model reasoning, factual correctness, or actions omitted
  from the trace.

See `tests/test_regression_gate.py` for unit-level behavior and
`docs/adversarial_alignment.md` for greedy-vs-optimal matching stress tests.

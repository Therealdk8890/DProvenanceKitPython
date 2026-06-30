# DProvenanceKit (Python)

**Reasoning observability and regression testing for AI systems — a Python port of the Swift [DProvenanceKit](https://github.com/Therealdk8890/DProvenanceKit).**

When an agent's reasoning drifts between runs, DProvenanceKit turns each execution into a queryable, diffable trace so you can see *what changed and why* — not just *what happened*.

> Run → Record → Query → Diff → Detect Regressions

This is a faithful, dependency-free port of the Swift library to Python. It keeps the same architecture and guarantees — synchronous non-blocking recording, priority-aware backpressure, one query language over two backends held at parity, structural diffing, formally-modeled semantic alignment, and by-tier drop accounting so load-shedding is never silent.

The original Swift package is unchanged; this is a parallel implementation.

---

## Why a Python port

The Swift library targets Apple-platform and on-device AI. This port brings the same reasoning-layer observability to Python codebases — agent frameworks, LLM workflows, tool-using models — with **zero third-party dependencies** (it uses only the standard library: `sqlite3`, `contextvars`, `threading`, `json`, `hashlib`, `uuid`, `urllib`).

---

## Install

From PyPI (released builds):

```bash
pip install dprovenancekit
pip install "dprovenancekit[langchain]"        # + LangChain adapter
pip install "dprovenancekit[openai-agents]"    # + OpenAI Agents adapter
```

From a checkout (development):

```bash
pip install -e ".[dev]"
```

Requires Python 3.9+; the core has **zero third-party dependencies**. Releasing is documented
in [RELEASING.md](RELEASING.md).

---

## 5-minute demo

### 1. Define your events

Any frozen dataclass that subclasses `TraceableEvent`, exposing a stable `type_identifier` and a `priority`:

```python
from dataclasses import dataclass
from dprovenancekit import TraceableEvent, TracePriority

@dataclass(frozen=True)
class MyAIDecision(TraceableEvent):
    kind: str           # "promptGenerated" | "documentEvaluated" | "conflictDetected" | "finalDecisionMade"
    token_count: int = 0
    document_id: str = ""
    score: float = 0.0
    reason: str = ""
    approved: bool = False

    @property
    def type_identifier(self) -> str:
        return self.kind

    @property
    def priority(self) -> TracePriority:
        if self.kind == "finalDecisionMade":
            return TracePriority.CRITICAL
        if self.kind == "conflictDetected":
            return TracePriority.DIAGNOSTIC
        return TracePriority.TELEMETRY
```

### 2. Record an execution run

`record(...)` is synchronous and never blocks — it touches only an in-memory buffer. Ambient run / engine / span context propagates through `contextvars`, so nested scopes attribute events correctly with no plumbing.

```python
from dprovenancekit import DProvenanceKit, InMemoryTraceStore

kit = DProvenanceKit(MyAIDecision)
store = InMemoryTraceStore()

with kit.run(context_id="demo_case", store=store):
    kit.record(MyAIDecision(kind="documentEvaluated", document_id="DocA", score=0.95))
    kit.record(MyAIDecision(kind="conflictDetected", reason="timeline_inconsistency"))
    kit.record(MyAIDecision(kind="finalDecisionMade", approved=False))
```

### 3. Query reasoning patterns

```python
from dprovenancekit import TraceQueryDSL

suspicious = store.query_runs(
    TraceQueryDSL()
        .requiring_step("conflictDetected")
        .missing_step("documentEvaluated")
)
```

Find runs where a conflict was reported but no document was ever evaluated. The same DSL compiles to SQL for `SQLiteTraceStore` and is evaluated in memory for `InMemoryTraceStore` — the two backends are held in lockstep by a parity test suite.

### 4. Diff runs

```python
from dprovenancekit import TraceDiffEngine

diff = TraceDiffEngine().diff(base=run_a, comparison=run_b)
print(diff.changes)   # structural steps that appeared, disappeared, or moved
```

### 5. Semantic alignment

`TraceAlignmentEngine` decides whether two executions are behaviorally equivalent within a formally-defined semantic model, even when payloads vary slightly:

```python
from dprovenancekit import (
    AlignmentConfiguration, AlignmentProfile, AnyEquivalenceEvaluator, TraceAlignmentEngine,
)

config = AlignmentConfiguration(
    profile=AlignmentProfile.strict_audit_v1,
    equivalence_evaluator=AnyEquivalenceEvaluator(
        evaluator_identifier="MyAIDecision_Semantic",
        evaluator=lambda a, b: 1.0 if a == b else 0.0,
    ),
)
result = TraceAlignmentEngine(config).align(base=run_a, comparison=run_b)
print(result.regression_risk.level)
```

### 6. Detect regressions automatically

```python
from dprovenancekit import AnomalyDetector, AnomalyRule, TraceQueryDSL

class UnverifiedConflictRule(AnomalyRule):
    @property
    def name(self): return "unverified_conflict"
    @property
    def anomaly_query(self):
        return TraceQueryDSL().requiring_step("conflictDetected").missing_step("documentEvaluated")
    def describe(self, run): return "Conflict detected with no supporting evaluation"

anomalies = AnomalyDetector(store).detect_anomalies([UnverifiedConflictRule()])
```

Or drop in ready-made rules from the built-in library instead of writing your own:

```python
from dprovenancekit import AnomalyDetector, LoopingRule, ToolDropRule

anomalies = AnomalyDetector(store).detect_anomalies([
    ToolDropRule("safety_check"),              # never ran a required step
    LoopingRule("web_search", max_repeats=5),  # stuck repeating the same tool call
])
```

### 7. Gate a pull request on regressions

Run the regression gate in CI with no server — point it at a local SQLite trace database
and a golden/candidate run id. Exit code is `0` (pass), `1` (regression), or `2` (usage error):

```bash
dprovenancekit gate --db traces.sqlite --golden "$GOLDEN_RUN_ID" --candidate "$CANDIDATE_RUN_ID"
dprovenancekit gate --db traces.sqlite --golden "$G" --candidate "$C" --max-level low --json

# Gate across separate databases (a restored baseline vs. this PR's run), resolving
# the golden run id from the baseline instead of hardcoding it:
GOLDEN=$(dprovenancekit runs --db baseline.sqlite --context my-agent --latest --format id)
dprovenancekit gate --golden-db baseline.sqlite --golden "$GOLDEN" \
                    --candidate-db candidate.sqlite --candidate "$CANDIDATE_RUN_ID"
```

Prebuilt CI integrations wrap this and comment the diff on the PR/MR:
a [GitHub Action](action/README.md) and a [GitLab CI template](gitlab/README.md).

---

## Benchmark corpus

The library ships the same validation corpus as the Swift version. The headless CLI runs it through the real benchmark runner:

```bash
dprovenancekit evaluate     # precision/recall/F1 over the standard + adversarial corpora
dprovenancekit diagnose     # causal ranking of failure modes
dprovenancekit stability    # determinism boundary: isolated vs perturbed F1 variance
```

Both corpora score **Precision 1.000 / Recall 1.000 / F1 1.000** — 8 standard scenarios (reordering, semantic evolution, noise injection, branch collapse, …) and 5 adversarial robustness traps (dependency inversion, partial truncation, semantic substitution, …) — matching the Swift implementation case-for-case.

---

## What's included

| Component | Module |
| --- | --- |
| Event model, priority tiers, drop accounting | `event`, `priority`, `drop_stats` |
| Recording API + ambient context | `kit`, `context` |
| Stores (in-memory, WAL SQLite, raw read, cloud) | `store`, `sqlite_store`, `raw_store`, `cloud_store` |
| Priority-aware write buffer | `write_buffer` |
| Query DSL + two backends (AST eval + SQL compiler) | `query` |
| Live querying + anomaly detection | `live_engine`, `anomaly` |
| Structural diff + span-aware snapshot diff | `diff`, `snapshot_diff` |
| Deterministic replay | `replay` |
| Semantic alignment engine + evidence + verification | `alignment_*`, `verification` |
| Benchmark harness, failure diagnoser, corpus | `benchmark`, `corpus` |
| Pure view models for a trace viewer | `viewmodel` |
| Framework adapters (LangChain / LangGraph) | `integrations.langchain` |
| Framework adapters (OpenAI Agents SDK) | `integrations.openai_agents` |
| Regression-gate test helper | `testing` |
| Framework-agnostic instrumentation (decorators) | `instrument` |

The SwiftUI `DProvenanceUI` target is intentionally **not** ported (it is Apple-platform UI); its pure value-model layer (`SpanViewModel`, flattening) is ported in `viewmodel`.

---

## Cross-language conformance

Keeping the Swift and Python SDKs behaviorally equivalent is enforced, not hoped for. [`conformance/`](conformance/) holds **Trace Specification v1** — a language-neutral contract plus frozen golden vectors that pin the run fingerprint, the alignment profile hash, canonical payload encoding, query semantics, and alignment verdicts.

```bash
python -m pytest tests/test_conformance.py   # the Python SDK's claim of conformance
python conformance/generate_vectors.py        # intentionally re-freeze the contract
```

The committed `conformance/vectors/*.json` are the contract: any SDK — Swift today, Rust or TypeScript later — proves equivalence by reproducing the same files. See [`conformance/TRACE_SPEC_v1.md`](conformance/TRACE_SPEC_v1.md).

---

## Integrations

Framework adapters live in `dprovenancekit.integrations` and are the only parts of the package with third-party dependencies — the core stays pure standard library, and nothing imports an adapter unless you do.

### LangChain / LangGraph

```bash
pip install dprovenancekit[langchain]
```

```python
from dprovenancekit import SQLiteTraceStore
from dprovenancekit.integrations.langchain import DProvenanceTracer, LangChainTraceEvent

store = SQLiteTraceStore(LangChainTraceEvent, "traces.sqlite")
tracer = DProvenanceTracer(store)

with tracer.trace(context_id="customer-42") as cb:
    answer = chain.invoke(question, config={"callbacks": [cb]})

# The run is now recorded — query it, diff it against a known-good run, or
# compare run fingerprints to detect when the agent took a different path.
```

[`DProvenanceCallbackHandler`](src/dprovenancekit/integrations/langchain.py) translates LangChain's callback stream into a trace: each `on_llm_start` / `on_tool_start` / `on_retriever_start` / `on_chain_start` (and its completion) becomes a typed event in execution order, LangChain's `run_id`/`parent_run_id` become the trace's **span tree**, the active model/tool/retriever becomes the **engine**, and (by default) lifecycle **provenance edges** are emitted (`DERIVED_FROM` start→completion, `INFORMED` parent→child). Because events flow through the same recording path as hand-written ones, the whole toolkit applies: a run's **fingerprint** is the structural identity of the agent's execution path, so two runs that diverge (a tool called in a different order, a retrieval step skipped) produce different fingerprints — a cheap regression signal. Options: `capture_payloads` (prompt/completion/IO previews), `link_lifecycle` (edges), `record_chains` (LCEL/LangGraph chain noise).

### OpenAI Agents SDK

```bash
pip install dprovenancekit[openai-agents]
```

```python
from dprovenancekit import SQLiteTraceStore
from dprovenancekit.integrations.openai_agents import register, OpenAIAgentsTraceEvent

store = SQLiteTraceStore(OpenAIAgentsTraceEvent, "traces.sqlite")
register(store)   # registers a global tracing processor

# ... run your agents normally; each run is recorded ...
```

[`DProvenanceTracingProcessor`](src/dprovenancekit/integrations/openai_agents.py) implements the SDK's `TracingProcessor`: each agent run becomes a trace-run (`context_id` = the trace name), and every span start/end becomes a typed event — `agent.start`, `generation.end`, `function.start`, `guardrail.error`, … — in execution order. The span's `span_id`/`parent_id` become the **span tree**, the active agent/tool/model becomes the **engine**, errors and triggered guardrails are recorded at `CRITICAL`, and lifecycle **provenance edges** are emitted (same `DERIVED_FROM`/`INFORMED` model). One registered processor captures every run; the same `fingerprint`/diff/align tooling then applies.

---

## Regression gate

`dprovenancekit.testing` turns "did my agent regress?" into one assertion you can drop into any test or CI step. Give it a *golden* run (known-good) and a *candidate* run (what your current code produced); it aligns them and fails with a readable diagnostic if the candidate diverged.

```python
from dprovenancekit.testing import assert_no_regression

assert_no_regression(golden=golden_run, candidate=candidate_run)
```

Strict by default — any removed, added, or changed (ambiguous) step fails, and a removed *or reordered* CRITICAL step is additionally a HIGH-severity regression (reordering a critical step can invert a dependency). Loosen with `max_regression_level` (gate only on severity) or `allow_divergent_steps` (tolerate benign per-step changes), or pass a custom `evaluator` to define what "equivalent" means (e.g. ignore volatile fields like token counts). `RegressionGate(...).check(...)` returns a `RegressionReport` (no raise) for richer assertions. Detecting *reordered* steps requires a span-aware profile (`AlignmentProfile.developer_debug_v1`); the default linear profile treats a pure reorder as still-matching. Complements `AlignmentSnapshotValidator` (an exact output-hash snapshot): the gate works on two runs and reasons about regression severity.

---

## Example: regression testing

[`examples/regression_testing.py`](examples/regression_testing.py) is the end-to-end story in ~150 readable lines: record a **golden** run of a fact-checking agent (retrieve → verify → decide), then catch a later run that skips its verification step — via both the fast **fingerprint** check and the detailed **alignment** verdict (which flags the dropped `claimVerified` step as a HIGH regression).

```bash
python examples/regression_testing.py
```

It self-asserts its verdicts, so it doubles as an executable test of the headline use case.

---

## Instrumenting plain code (no framework)

Not using a framework? Instrument a hand-written agent loop directly — no event type to define, zero dependencies (ships in core as `dprovenancekit.instrument`):

```python
from dprovenancekit import InMemoryTraceStore, traced, traced_run, record_event

@traced
def search(query): ...

@traced
def answer(question, sources): ...

store = InMemoryTraceStore()
with traced_run(store, context_id="ticket-42"):
    sources = search(question)
    record_event("plan.chosen", {"strategy": "rag"})
    reply = answer(question, sources)
```

`@traced` records a `"<name>.start"` / `".end"` / `".error"` event pair per call in its own **span** (the function name is the **engine**), nests calls in the span tree, and emits the same `DERIVED_FROM` / `INFORMED` provenance edges as the framework adapters. `record_event(...)` drops an ad-hoc event (a decision, a chosen branch). Plain functions, `async def`, generators, and async generators are all supported (for a generator, start/end bracket the full iteration). Instrumentation never changes behavior — capture is failure-proof and exceptions pass through unchanged. Outside a `traced_run` the decorators are transparent, so instrumented code is safe to call untraced. The trace it produces is identical in shape to the adapter-produced ones, so fingerprint / diff / align / the regression gate all apply.

---

## Tests

```bash
python -m pytest
```

168 tests: 80 ported from the Swift suite (query parity, write-buffer backpressure, SQLite stress + drop accounting, alignment, replay, snapshot diff, explainability fidelity, benchmark scoring, cloud chaos, …), 28 cross-language conformance checks against the frozen Trace Specification v1 vectors, 14 LangChain integration tests, 16 OpenAI Agents SDK integration tests, 16 instrumentation-layer tests, 13 regression-gate tests, and the regression-testing example run as a self-asserting test. (The real-framework tests run only when `langchain-core` / `openai-agents` are installed, otherwise skipped.)

---

## License

Distributed under the **Apache License 2.0**. See [LICENSE](LICENSE).

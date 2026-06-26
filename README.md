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

```bash
pip install -e .
# optional, for the tests:
pip install -e ".[dev]"
```

Requires Python 3.9+.

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

---

## Benchmark corpus

The library ships the same validation corpus as the Swift version. The headless CLI runs it through the real benchmark runner:

```bash
dprovenancekit evaluate     # precision/recall/F1 over the standard + adversarial corpora
dprovenancekit diagnose     # causal ranking of failure modes
dprovenancekit stability    # determinism boundary: isolated vs perturbed F1 variance
```

The standard corpus scores **Precision 1.000 / Recall 1.000 / F1 1.000** across 8 scenarios (reordering, semantic evolution, noise injection, branch collapse, …), matching the Swift implementation.

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

The SwiftUI `DProvenanceUI` target is intentionally **not** ported (it is Apple-platform UI); its pure value-model layer (`SpanViewModel`, flattening) is ported in `viewmodel`.

---

## Tests

```bash
python -m pytest
```

80 tests, ported from the Swift suite (query parity, write-buffer backpressure, SQLite stress + drop accounting, alignment, replay, snapshot diff, explainability fidelity, benchmark scoring, cloud chaos, …).

---

## License

Distributed under the **Business Source License 1.1**, same as the upstream Swift project. See [LICENSE](LICENSE).

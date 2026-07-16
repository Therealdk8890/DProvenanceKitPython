# AGENTS.md — DProvenanceKit (Python)

A **faithful, dependency-free Python port** of the Swift DProvenanceKit: reasoning-observability and
regression testing for AI systems. It keeps the same architecture and guarantees as the Swift library —
that fidelity is the point. Preserve the invariants and the parity with upstream.

## Stack
- setuptools, **flat layout** — the package lives at `dprovenancekit/` in the repo root (no `src/`). **Python 3.9+.**
- Version **0.6.x** — `pyproject.toml` is the source of truth. `__version__` derives from installed
  package metadata (`importlib.metadata`), with a hardcoded fallback literal in
  `dprovenancekit/__init__.py` for source checkouts; the release checklist in `RELEASING.md` bumps both.
- **Zero third-party runtime dependencies in the core** — standard library only (`sqlite3`, `contextvars`, `threading`, `json`, `hashlib`, `uuid`, `urllib`). **Do not add any core runtime dependency.** Framework adapters (LangChain, OpenAI Agents, LlamaIndex, CrewAI, Google GenAI, FastAPI, Jupyter, MCP) live behind optional extras; `pytest` and friends are dev-only extras.
- CLI entry point `dprovenancekit` (`dprovenancekit.cli:main`). Subcommands: `demo` (run the installed zero-dependency end-to-end regression tour), `gate` (fail the build when a candidate run regresses against a golden run; CI-friendly exit codes), `runs` (list/select recorded runs, e.g. baseline lookup), `anomalies` (run JSON-configured anomaly rules over a store), `ui` (local trace-visualization server, stdlib `http.server`), `ingest` (import OTLP JSON traces into a store), `export` (dump runs as JSONL for log pipelines), `sync` (push/pull runs to the hosted dashboard — requires the premium `dprovenancekit-cloud` package; exits 2 with an install hint without it), plus the benchmark modes `evaluate` / `diagnose` / `stability`.
- Also ships a pytest plugin (`dprovenancekit.pytest_plugin`, registered via the `pytest11` entry point).
- License **Apache-2.0** (see `LICENSE` and `NOTICE`). This public repo is the open-source SDK; the paid server lives in a separate private repo — don't add server code here.
- Python 3.9 floor → no 3.10+ syntax in runtime code (no `match`, no `X | Y` unions; use `Optional[...]`/`Union[...]`). Type hints throughout; events are frozen dataclasses.

## Invariants — do not break (ported from Swift; keep in lockstep)
1. **Recording never blocks.** `record(...)` is synchronous, in-memory buffer only — never waits on disk. Event queryable the instant it returns. No I/O on the record path.
2. **Backpressure is priority-aware and O(1).** One FIFO per tier (`critical`/`structural`/`diagnostic`/`telemetry`); ingestion and shedding stay constant-time at capacity — no backlog scan. Shed `telemetry`/`diagnostic` first; preserve `structural`/`critical`. Diffs floored at `structural`.
3. **Dropping is never silent.** Every dropped event tallied by tier (incl. payloads that fail to encode); `drop_stats.preserved_integrity` must stay meaningful.
4. **Two backends at parity.** `TraceQueryDSL` evaluates in memory (`InMemoryTraceStore`) **and** compiles to SQL (`SQLiteTraceStore`). Any query-semantics change lands in both and is verified by `tests/test_query_parity.py`.
5. **Crash-safe persistence.** WAL-mode SQLite, reconciliation on open, structural fingerprint per run. Don't weaken it.
6. **Ambient context via `contextvars`.** Run / engine / span context propagates through `contextvars` (the Python analogue of Swift's `@TaskLocal`). See ContextVar safety below.
7. **SQL safety.** Parameterized `sqlite3` placeholders only — never f-string / `%`-format SQL.
8. **Stable identity.** `type_identifier` stays stable across schema versions.

## Python-specific hazards (recently hardened — keep them fixed)
- **ContextVar safety.** Always `reset(token)` in a `finally` so run/engine/span context can't leak across runs or threads. Don't capture-and-stash context tokens; scope them.
- **Resource teardown.** Stores, SQLite connections, and the background writer must close deterministically (context managers / explicit `close()`). Guard every engine/connection use against a torn-down or `None` engine — don't operate on a closed/null engine.
- **Thread safety.** The background writer uses `threading`; keep the synchronous in-memory commit and the background drain race-free. Don't add shared mutable state without a lock.
- **Quiet logging.** The library must not configure root logging or attach noisy handlers. Keep logging side-effect-free for importers.

## Port scope & parity with Swift
- The SwiftUI `DProvenanceUI` target is **intentionally not ported** (Apple-platform UI). Only its pure value-model layer is ported, in `viewmodel`. Don't port UI.
- This is a parallel implementation of the upstream Swift library; the Swift package is unchanged. When semantics change on either side, **mirror the change** and keep `tests/test_query_parity.py` and the corpus green. Corpus precision/recall/F1 must match the Swift implementation (currently 1.000).

## Commands
- Install (editable): `pip install -e .` · with dev tools: `pip install -e ".[dev]"`
- Test: `python -m pytest`
- Parity suite (run on any query/store change): `python -m pytest tests/test_query_parity.py`
- Targeted test: `python -m pytest tests/test_<area>.py::<TestClass>::<test_method>`
- Drop accounting: `python -m pytest tests/test_sqlite_encode_drop.py tests/test_sqlite_insert_failure_drop.py`
- Benchmarks: `dprovenancekit evaluate` · `dprovenancekit diagnose` · `dprovenancekit stability` (equivalently `python -m dprovenancekit.cli <mode>`)
- Installed demo: `dprovenancekit demo` (writes its self-contained artifacts to the current directory) · choose another location with `--output-dir`
- CI gating: `dprovenancekit gate --db traces.db --golden <run-id> --candidate <run-id>` (exit 0 pass / 1 regression / 2 usage error) · baseline lookup: `dprovenancekit runs --db traces.db --context <ctx> --latest --format id` · anomaly rules: `dprovenancekit anomalies --db traces.db --rules rules.json`
- Local trace UI: `dprovenancekit ui --db traces.db --port 8080`
- Lint/type gate (CI-enforced, must stay clean): `ruff check dprovenancekit/` · `mypy dprovenancekit/`
- Syntax check: `python -m compileall dprovenancekit`

## Definition of done
`python -m pytest` green — `tests/test_query_parity.py` in particular — `ruff check dprovenancekit/`
and `mypy dprovenancekit/` clean, corpus scores not regressed, no new runtime dependency introduced,
and the ContextVar / teardown / threading / logging hazards above respected.

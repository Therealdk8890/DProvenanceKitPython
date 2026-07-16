# Changelog

All notable changes to `dprovenancekit` are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). While the library is `0.x`, the
public API may still change between minor versions.

## [Unreleased]

## [0.6.1] - 2026-07-15

### Security

- **Local trace viewer binds to `127.0.0.1` by default.** `dprovenancekit ui` previously bound
  all interfaces while printing a `localhost` URL, exposing recorded prompts and outputs to the
  local network. It now defaults to loopback with an explicit `--host` flag to opt out.
- **Trace data is HTML-escaped in the viewer.** The trace viewer rendered payloads, context ids,
  engine names, and error strings without escaping, allowing stored XSS from LLM/tool-controlled
  trace data. All rendered values are now escaped.
- **CI-comment renderers escape trace-derived text.** The GitHub Action PR comment and GitLab MR
  note escaped neither the column separator nor newlines in trace-derived step names, letting a
  hostile trace inject markdown (e.g. a forged "gate passed" banner). Step names and reasoning are
  now sanitized.
- **GitHub Action passes caller inputs via `env:`** instead of interpolating them into inline
  shell, removing a script-injection vector.
- **The GitLab CI template no longer tracks `main`.** The remote `include:` and the `mr_note.py`
  fetch are pinned to a release tag via a new `DPROV_REF` variable, so an upstream push can't change
  what runs in a consumer's pipeline.

### Fixed

- **The local trace viewer renders real run and event dates.** SQLite run metadata is stored in
  microseconds while detailed events use seconds; the viewer now normalizes both (plus ordinary
  JavaScript milliseconds) instead of displaying `Invalid Date` or a raw epoch.
- **The end-to-end demo writes artifacts where the user ran it.** It no longer writes ignored
  databases and reports beside the checkout script, and its printed follow-up commands shell-quote
  paths safely.
- **CLI exits `2` on an unknown subcommand** (previously printed usage and exited `0`, which could
  let a typo silently pass a CI gate). Added top-level `--help` and `--version`.
- **OTLP ingestion keeps GenAI spans nested under a non-GenAI root span.** Traces from an
  auto-instrumented service (an HTTP/gRPC root wrapping GenAI work) previously ingested zero runs.
- **`SQLiteTraceStore.query_runs` flushes pending writes first,** so runs recorded immediately
  before a query are visible (read-your-writes), removing a source of flaky gate failures.
- **The `sync` CLI command and nonexistent-database paths** now report a clean error and exit `2`
  instead of raising a traceback or silently creating an empty database file.
- **The write buffer's retry queue is now bounded.** Under a persistently failing writer (e.g. a
  locked database) it previously grew without limit while reporting zero depth; it is now capped at
  the configured capacity, sheds the oldest retried rows (counted as drops), and its backlog is
  reflected in the buffer's depth signal.
- **`SQLiteTraceStore.get_events` chunks its query,** so fetching a graph with more than ~999 nodes
  no longer raises "too many SQL variables" on the older SQLite versions some LTS distros ship.

### Added

- **`dprovenancekit demo` ships in the installed wheel.** A zero-configuration tour records a
  healthy and regressed agent run, queries and diffs them, applies the gate and anomaly rules, then
  writes a self-contained SQLite database, JSON ruleset, and shareable HTML report with copy-paste
  commands for the CLI and local viewer.
- **`dprovenancekit gate --profile {strict_audit_v1,developer_debug_v1}`** exposes the alignment
  profile, making reorder detection reachable from the CLI (the span-aware profile fails on
  reordered critical steps; the default remains linear).
- **`LoopingRule` supports per-tool detection** via `engine=` (scope the count to one tool) and
  `per_engine=True` (flag any single tool exceeding the threshold), instead of only a global
  tool-call budget. The bundled `agent.default` ruleset uses per-tool counting.
- **`--max-level low|medium`** now warns that they currently behave like `none` (the engine emits
  only `none`/`high` today) rather than silently no-op'ing.
- **The `anomalies` command reports each finding's severity and message** in both text and JSON.
- **Continuous integration adds `ruff` and `mypy` gates**, publishes only after the test suite
  passes, and surfaces skipped tests in the logs.
- **Releases ship with GitHub artifact attestations (SLSA build provenance).** The publish
  workflow attests `dist/*`, so downloaded artifacts can be checked with
  `gh attestation verify` — alongside the PEP 740 attestations that PyPI trusted publishing
  already uploads. Documented in `RELEASING.md`.

### Changed

- **The local viewer run picker is keyboard- and assistive-technology friendly.** Runs are native
  buttons with visible focus and pressed state, and comparison selectors have programmatic labels.
- **The public API (`__all__`) was curated from 178 symbols to 87.** Internal machinery (query
  compiler/planner, write buffer, sqlite connection/writer, alignment-engine internals, benchmark
  report types, verification invariants, Swift-port view/perturbation helpers) is no longer in the
  curated surface. This is **non-breaking**: every one of those symbols is still importable via
  `from dprovenancekit import X`; only `from dprovenancekit import *` and tooling's notion of the
  public surface change.
- **`UnusedToolResultRule` is parallel-safe:** it no longer false-positives when a framework emits
  multiple tool results before the next model call (parallel/fan-out tool use).
- **The `google_genai` and `llama_index` adapters were hardened** to the standard of the other
  adapters: defensive attribute extraction, correct span lineage under async/parallel execution,
  bounded payload capture, `CRITICAL`-priority error events, and model/component-derived engine
  names. Note: runs recorded by the new adapters use different engine names than the old ones, so
  diffs across the upgrade will show an engine change.

## [0.6.0]

Baseline for this changelog. See the Git history and
[GitHub releases](https://github.com/Therealdk8890/DProvenanceKitPython/releases) for earlier
changes.

[Unreleased]: https://github.com/Therealdk8890/DProvenanceKitPython/compare/v0.6.1...HEAD
[0.6.1]: https://github.com/Therealdk8890/DProvenanceKitPython/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/Therealdk8890/DProvenanceKitPython/releases/tag/v0.6.0

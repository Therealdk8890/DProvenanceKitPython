# Changelog

All notable changes to `dprovenancekit` are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). While the library is `0.x`, the
public API may still change between minor versions.

## [Unreleased]

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

### Fixed

- **CLI exits `2` on an unknown subcommand** (previously printed usage and exited `0`, which could
  let a typo silently pass a CI gate). Added top-level `--help` and `--version`.
- **OTLP ingestion keeps GenAI spans nested under a non-GenAI root span.** Traces from an
  auto-instrumented service (an HTTP/gRPC root wrapping GenAI work) previously ingested zero runs.
- **`SQLiteTraceStore.query_runs` flushes pending writes first,** so runs recorded immediately
  before a query are visible (read-your-writes), removing a source of flaky gate failures.
- **The `sync` CLI command and nonexistent-database paths** now report a clean error and exit `2`
  instead of raising a traceback or silently creating an empty database file.

### Added

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

### Changed

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

[Unreleased]: https://github.com/Therealdk8890/DProvenanceKitPython/compare/v0.6.0...HEAD
[0.6.0]: https://github.com/Therealdk8890/DProvenanceKitPython/releases/tag/v0.6.0

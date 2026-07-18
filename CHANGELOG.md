# Changelog

All notable changes to `dprovenancekit` are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). While the library is `0.x`, the
public API may still change between minor versions.

## [Unreleased]

### Security

- **The standalone HTML visualizer now escapes trace data.** `render_trace_html`
  (`visualizer.py`) interpolated payloads, engine names, type ids, and the title into HTML and
  into an inlined `<script>` JSON block with no escaping — the same stored-XSS class 0.6.1 fixed
  in the served viewer and PR/MR renderers, but this module was missed. Server-rendered values are
  now HTML-escaped, the embedded JSON is escaped so a `</script>` in trace data cannot break out,
  and the client-side inspector escapes values before assigning `innerHTML`.
- **The GitHub Action regression gate can no longer be bypassed by a crafted trace.**
  `action/run_gate.py` wrote the multi-line `summary` (which embeds attacker-influenceable step
  `type_identifier`s) to `$GITHUB_OUTPUT` with a fixed heredoc delimiter, so a candidate step name
  containing that delimiter line plus a forged `passed=true` could close the heredoc early and
  override the real verdict — silently passing a real regression. Each value now uses a random
  per-write delimiter verified absent from the content. The `$GITHUB_STEP_SUMMARY` code fence is
  likewise sized to exceed any backtick run it encloses, so a step name cannot inject markdown.
- **The LlamaIndex adapter redacts secrets from captured payloads.** With payload capture on
  (the default) it stringified every non-structural value, including `EventPayload.SERIALIZED` —
  the serialized LLM config, which has shipped an `api_key` in some llama-index versions — into a
  trace store this toolkit encourages committing as a golden baseline. Secret-keyed values, nested
  ones included, are now replaced with a redaction placeholder while non-secret structure (model
  name, token counts) is preserved.
- **The local trace viewer validates the `Host` header on loopback binds.** The API returned
  trace prompts/outputs with no `Host`/`Origin` check, so a malicious page could reach it via DNS
  rebinding (rebinding its hostname to `127.0.0.1`). A loopback-bound viewer now rejects requests
  whose `Host` isn't loopback; an explicit non-loopback `--host` bind leaves filtering off.

### Fixed

- **`RegressionRisk` no longer misses materially-changed or skipped critical steps.** The risk
  verdict was derived only from `removed`/`reordered` alignment states, but a critical step that
  was changed or skipped binds to a same-type event (type match alone clears the matcher
  threshold), is classified `ambiguous`, and so escaped the verdict entirely — a tampered or
  skipped critical decision reported `RegressionLevel.NONE` on the flagship `strict_audit_v1`
  profile, the exact regressions the engine exists to catch (SEMANTICS Def 5 / Invariant A/E).
  The verdict is now derived from the equivalence outcome: a critical step that is removed, bound
  below the profile's `semantic_threshold`, or reordered relative to another **critical** step
  fires HIGH. Reorder is computed over critical pairs only, so a benign structural/diagnostic step
  moving past a stationary critical no longer fires a false HIGH. Alignment states and every
  calibrated corpus case are unchanged; this only corrects the risk level. Behavior change: runs
  that previously (incorrectly) reported `NONE` may now report `HIGH` (e.g. a `RegressionGate`
  with `allow_divergent_steps=True` now fails on a flipped critical decision unless
  `max_regression_level` is raised).
- **Nested boolean queries return correct results on the SQLite backend.** The query compiler
  joined compound `AND`/`OR`/`missing_step` members with bare `INTERSECT`/`UNION`/`EXCEPT`; SQLite
  gives those operators equal, left-to-right precedence, so a nested member was silently re-grouped
  and diverged from the in-memory evaluator (e.g. `has(a) OR missing(b)` returned the wrong runs).
  Each compiled member is now isolated in a sub-select.
- **The write buffer no longer over-sheds a run's events after a capacity burst.** The per-run depth
  counter was written back from a value captured before global-capacity eviction, so evicting a row
  from the enqueuing run left the counter permanently inflated, spuriously tripping the soft per-run
  cap. The counter now tracks actual occupancy.
- **Early-terminating a `@traced` generator records a normal end, not a CRITICAL error.** Breaking
  out of a traced generator (or async generator) raised `GeneratorExit`, which was recorded as a
  spurious CRITICAL `.error`, diverging partially-consumed streams from fully-consumed ones and
  tripping error-keyed anomaly rules. It now records `.end`.
- **The google-genai wrapper nests calls under the enclosing span.** It set only the current span,
  leaving the parent pointing at the enclosing span's parent (the grandparent), so nested
  `generate_content` calls attached to the wrong node. It now sets the parent span too.
- **The alignment engine detects reordering under every profile, keyed on sequence.** Reorder
  detection was suppressed in `LINEAR` mode, so the strictest audit profile (`strict_audit_v1`)
  never flagged critical-step reordering that the debug profile caught; and it compared list
  position rather than the authoritative `sequence`, flagging logically identical but unsorted
  runs as a spurious HIGH regression. `align()` now sorts by sequence and reorder detection is
  mode-independent.
- **Trace replay surfaces span-cycle events as orphans instead of dropping them.** A parent cycle
  (`A↔B`) or self-parent left its spans neither rooted nor orphaned, so their events vanished from
  the reconstructed tree while the manifest still counted them. Unreachable spans are now reported
  as orphaned events.
- **The trace-graph cycle validator catches cycles on partial graphs and survives deep chains.** It
  seeded the search only from `graph.nodes`, missing cycles among nodes that appear solely in edges
  (as `lineage()`/`impact()` can produce), and recursed per hop so a long valid causal chain raised
  `RecursionError`. The search now seeds from all edge endpoints and is iterative.
- **`UnregisteredToolRule` no longer fails open on a string registry.** When the registry field was
  a bare string rather than a list, `tool_name not in registry` degraded to substring matching, so
  an unregistered tool whose name was a substring of the registry string was treated as allowed. A
  string registry is now compared as a single exact entry (fail closed).
- **The in-memory live-subscription consumer survives a raising subscriber.** A subscriber callback
  raising once killed the shared daemon consumer thread, silently stopping *all* live delivery while
  `record` kept enqueuing into an unbounded queue. The consumer now logs and continues.
- **Framework adapters and the SQLite writer log previously-silent failures.** The CrewAI listener
  swallowed every translation error with no trace (a version whose events lack the assumed
  correlation fields produced empty traces with no clue why); `SQLiteTraceStore.flush` swallowed a
  failed runs-table write that leaves events durable but unreadable. Both now log while preserving
  the non-fatal behavior.
- **The trace viewer serializes payloads via `to_dict()`.** `_json_serializable` checked `__dict__`
  first, which dataclasses always have, so the `to_dict()` branch was dead and the viewer showed
  internal field names instead of each payload's canonical, export-consistent shape.

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

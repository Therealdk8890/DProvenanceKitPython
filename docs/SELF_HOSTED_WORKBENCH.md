# Self-hosted multi-database workbench

## Objective

Extend the existing local Python trace viewer from one SQLite database to an explicitly configured set of local trace databases. Keep the viewer useful without a hosted service, commercial account, or network upload.

## Repository facts

The current `dprovenancekit.ui_server` already provides:

- a local HTTP server with loopback binding by default;
- a Host-header allow-list to mitigate DNS rebinding;
- run listing and run-detail endpoints;
- strict-audit diffing between runs in the configured database;
- an HTML viewer that selects runs and compares traces.

The current server factory accepts one `db_path`. The existing viewer therefore does not yet provide a unified index across independent database files.

The Python SDK already has framework adapters for LangChain, LlamaIndex, OpenAI Agents, CrewAI, and other integrations. This work should not duplicate those adapters.

## Proposed architecture

### 1. Explicit source registry

Add a small immutable source configuration model:

- `source_id`: stable caller-supplied identifier, unique within the server;
- `label`: human-readable label shown in the UI;
- `db_path`: local filesystem path to a SQLite database.

The existing `create_server(db_path=...)` and `run_ui_server(db_path=...)` calls must remain valid and behave exactly as the single-source mode does today. Multi-source mode should be opt-in through an explicit list of source configurations. Do not scan arbitrary directories or discover databases implicitly.

### 2. Source-aware API

In multi-source mode:

- `GET /api/sources` reports each configured source and its health without exposing absolute filesystem paths.
- `GET /api/runs` aggregates run metadata and adds `source_id` and `source_label` to each row.
- `GET /api/runs/{run_id}?source={source_id}` reads a run from the named source.
- `GET /api/diff?golden={id}&golden_source={source}&candidate={id}&candidate_source={source}` compares runs from either the same or different databases.

Single-source callers that omit source parameters retain the current behavior.

A duplicate run UUID across two configured databases must remain unambiguous because every multi-source detail/diff request identifies its source. Never silently choose the first matching database.

### 3. Failure isolation and read-only behavior

- Treat every configured database as read-only input.
- A missing, unreadable, malformed, or schema-incompatible database must produce a visible source-health error while healthy sources remain available.
- Do not silently omit a source from the source list or run index.
- Do not create, migrate, checkpoint, or otherwise mutate source files as part of viewer reads. Verify the existing SQLite store open behavior before selecting the read-only connection strategy.
- Preserve the loopback default and Host-header allow-list. A Docker deployment must document that non-loopback exposure requires an explicit security boundary.

### 4. UI behavior

- Display source labels beside runs.
- Preserve run selection and side-by-side comparison.
- Allow comparisons across sources.
- Show source errors and empty sources separately; do not conflate an unreadable database with a database containing zero runs.
- Escape all trace-derived and source-derived strings before rendering HTML.

### 5. Docker packaging

After the multi-source API and UI are tested:

- Add a minimal Docker image and Compose example.
- Mount the trace directory explicitly as read-only.
- Bind the service to loopback by default on the host.
- Document that the viewer exposes sensitive prompts, tool inputs, and outputs; users must not publish it directly to the internet.
- Keep the container dependency footprint minimal and pin the base image/runtime deliberately.

## Security and verification invariants

1. Strict-audit alignment remains the default for comparisons.
2. Semantic alignment may assist investigation, but must never authorize a match or downgrade a strict-audit finding.
3. Source identity is part of the comparison context and is included in exported/report metadata.
4. The viewer performs no uploads, telemetry, or automatic network calls.
5. Existing single-database endpoint behavior remains backward compatible.
6. Source database bytes and metadata are not modified by viewer reads.

## Delivery sequence

1. Add source registry and source-health model with unit tests.
2. Add source-aware run listing and run-detail routing.
3. Add cross-source strict-audit diffing and duplicate-ID tests.
4. Update the HTML viewer with source labels, cross-source comparison, and source-health status.
5. Add security and read-only behavior tests.
6. Add Dockerfile, Compose example, and local-only documentation.
7. Run the complete Python test/lint/type-check matrix and review all CI results before merging.

## Acceptance criteria

- Existing single-database tests pass unchanged.
- Multiple configured databases appear in one viewer with stable source labels.
- Runs can be inspected and compared across different databases.
- Duplicate UUIDs are handled deterministically through explicit source identity.
- An unhealthy database does not take down healthy sources.
- Read-only behavior and loopback/Host protections have automated regression tests.
- Docker/Compose works with explicitly mounted local databases and does not require a hosted control plane.

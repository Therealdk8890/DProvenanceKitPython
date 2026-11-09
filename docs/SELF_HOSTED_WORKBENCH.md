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

### CLI usage

Single-database mode remains unchanged:

```bash
dpk ui --db .dprovenance/traces.sqlite
```

Multi-database mode is explicit; repeat `--source ID=LABEL=PATH` for each local database:

```bash
dpk ui \
  --source prod=Production=/data/production.sqlite \
  --source staging=Staging=/data/staging.sqlite
```

Source IDs must be unique and use letters, digits, dots, underscores, or hyphens. Labels are display-only. The viewer opens each path read-only and does not copy or upload trace contents.

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
- Do not create, migrate, checkpoint, or otherwise mutate source files as part of viewer reads.
- Do not use `SQLiteTraceStore(..., start_writer=False)` as the read path: its constructor still runs `_create_schema()` (including DDL, index creation, schema-version updates, and write-behind reconciliation), and `close()` calls writer shutdown/flush.
- Implement a dedicated read-only reader using SQLite URI `mode=ro` (with URI-safe path construction), and query the existing schema without invoking store initialization or writer lifecycle methods. Validate schema compatibility with read-only metadata queries before serving a source.
- Preserve the loopback default and Host-header allow-list. Non-loopback binds require an explicit allow-list when used behind the loopback-only Docker port mapping.

### 4. UI behavior

- Display source labels beside runs.
- Preserve run selection and side-by-side comparison.
- Allow comparisons across sources.
- Show source errors and empty sources separately; do not conflate an unreadable database with a database containing zero runs.
- Escape all trace-derived and source-derived strings before rendering HTML.

### 5. Docker packaging

The repository includes a minimal Python 3.12 / Debian Bookworm image and a Compose deployment:

```bash
mkdir -p traces
# Place an existing trace database at ./traces/traces.sqlite
docker compose up --build
```

Open `http://localhost:8080`. The Compose configuration:

- publishes the port only on host loopback (`127.0.0.1:8080`);
- mounts `./traces` at `/traces` as read-only;
- runs with a read-only container filesystem, a temporary `/tmp`, no Linux capabilities, and `no-new-privileges`;
- binds inside the container to `0.0.0.0` but retains an explicit Host allow-list for `localhost`, `127.0.0.1`, and `::1` to block DNS-rebinding Host headers.

The viewer exposes sensitive prompts, tool inputs, and outputs. Do not publish the port on a public interface or attach the container to an untrusted shared Docker network. The host-loopback port binding does not make an untrusted peer on the same Docker network trustworthy. The base image is pinned to the Python 3.12 slim Bookworm line; update it deliberately and re-run the Python/Docker validation matrix.

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
7. Run the complete Python test/lint/type-check matrix, validate the Docker build/Compose config, and review all CI results before merging.

## Acceptance criteria

- Existing single-database tests pass unchanged.
- Multiple configured databases appear in one viewer with stable source labels.
- Runs can be inspected and compared across different databases.
- Duplicate UUIDs are handled deterministically through explicit source identity.
- An unhealthy database does not take down healthy sources.
- Read-only behavior and loopback/Host protections have automated regression tests.
- Docker/Compose works with explicitly mounted local databases and does not require a hosted control plane.

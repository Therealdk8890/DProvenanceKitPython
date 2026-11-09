# Self-hosted trace workbench

The Python SDK has a local web viewer for existing DProvenanceKit SQLite trace databases. The published `0.9.2` package contains the original single-database viewer. The multi-database workbench described below is included in the `0.9.3` release candidate and will be available from PyPI after that release is published.

## Install

For the currently published single-database viewer:

```bash
python -m pip install "dprovenancekit==0.9.2"
dpk ui --db .dprovenance/traces.sqlite
```

To use the `0.9.3` candidate's multi-database mode before PyPI publication, install the current repository checkout:

```bash
git clone https://github.com/Therealdk8890/DProvenanceKitPython.git
cd DProvenanceKitPython
python -m pip install .
```

The viewer binds to `127.0.0.1` by default. Open [http://localhost:8080](http://localhost:8080).

## Multiple databases

Configure each source explicitly. Repeat `--source ID=LABEL=PATH`:

```bash
dpk ui \
  --source prod=Production=/data/production.sqlite \
  --source staging=Staging=/data/staging.sqlite
```

- IDs must be unique and contain only letters, digits, dots, underscores, or hyphens.
- Labels are display names; paths point to existing SQLite trace databases.
- The viewer does not scan directories, create databases, or upload data.
- A source that is missing, unreadable, or schema-incompatible is reported as unhealthy without taking healthy sources offline.
- Run details and comparisons are source-aware, including when two databases contain the same run UUID.
- Existing single-database `--db` usage remains supported.

## Docker Compose deployment

The repository includes a hardened local container configuration. From a checkout:

```bash
mkdir -p traces
# Copy an existing DProvenanceKit SQLite database to ./traces/traces.sqlite
docker compose up --build
```

Open [http://localhost:8080](http://localhost:8080). The Compose configuration:

- publishes the port only on host loopback (`127.0.0.1:8080`);
- mounts `./traces` read-only at `/traces`;
- runs as a non-root numeric user with a read-only container filesystem;
- drops Linux capabilities and enables `no-new-privileges`;
- retains an explicit Host allow-list to mitigate DNS rebinding.

The image is built from the repository checkout; Docker is not required for the pip-installed single-database viewer.

## Security model and limitations

The viewer exposes trace contents, which may include prompts, tool inputs, outputs, and sensitive business or personal data.

- Keep the default loopback bind unless you have deliberately designed and secured a network deployment.
- The viewer does **not** provide user authentication or authorization. A Host allow-list is a DNS-rebinding defense, not authentication.
- Do not publish the port on a public interface or attach the container to an untrusted shared Docker network.
- Source databases are opened with SQLite URI `mode=ro`; viewer reads do not invoke the store's schema initialization or writer lifecycle.
- API responses disable caching and MIME sniffing. Trace- and source-derived strings are escaped before HTML rendering.
- The viewer performs no telemetry, upload, or automatic network calls.

## API overview

In multi-source mode:

- `GET /api/sources` reports configured source health without returning filesystem paths.
- `GET /api/runs` aggregates run metadata and attaches source IDs and labels.
- `GET /api/runs/{run_id}?source={source_id}` loads a run from a named source.
- `GET /api/diff?golden={id}&golden_source={source}&candidate={id}&candidate_source={source}` compares runs from either the same or different databases.

Strict-audit alignment remains authoritative. The viewer does not turn semantic suggestions into verification or claim that recorded content is true.

## Package validation

The CI workflow builds a wheel from the current repository revision, installs it in a fresh virtual environment, imports the viewer module, verifies that the packaged `index.html` resource exists, and invokes `dpk ui --help`. This verifies packaging of the `0.9.3` release candidate; it does not claim that the already-published `0.9.2` wheel contains the newer multi-database implementation.

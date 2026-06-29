# DProvenanceKit — hosted backend

The **managed service** for DProvenanceKit. The library stays the free, BSL-licensed
client; this is the server it talks to, plus the layer worth paying for: a **regression
gate** API and a dashboard.

It speaks the Trace Specification v1 cloud wire format (§7), so the library's
[`CloudTraceStore`](../src/dprovenancekit/cloud_store.py) works against it **unchanged**:

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/capabilities` | schema/feature negotiation |
| `POST` | `/ingest` | accept a batch of trace events (Bearer auth) |
| `POST` | `/query` | run a wire-form query DSL (Trace Spec §6) |
| `GET` | `/api/runs`, `/api/runs/{id}` | runs + fingerprints for the dashboard |
| `POST` | `/api/gate` | **regression gate**: golden vs candidate → verdict |
| `GET` | `/` | the dashboard |

Everything is generic over any consumer payload (events stored type-erased as
`AnyTraceableEvent`), and the whole reasoning layer — query DSL, run fingerprint, semantic
alignment, the regression gate — is **reused verbatim from the library**, so the service and
the SDK can never drift. Dependencies: **none beyond the standard library** + `dprovenancekit`.

## Run it

```bash
python server/run.py                       # http://127.0.0.1:8787  (dashboard at /)
DPROV_STORAGE=sqlite python server/run.py  # durable; prints a seeded API key on first run
```

On first run in the default (tenancy) mode it seeds a `demo` project and prints an API key
**once** — paste it into the dashboard. For local dev you can skip tenancy with a static key
map: `DPROV_API_KEYS="demo-key:demo" python server/run.py`.

## Storage

`DPROV_STORAGE` selects the backend; both are held at **parity** by the library's test suite,
so the query/gate code is identical either way:

- `memory` (default) — in-process; great for dev and tests.
- `sqlite` — one WAL SQLite file per project under `DPROV_DATA_DIR` (default `./dprov-data`);
  durable across restarts.

## Auth & tenants

Production uses a durable, multi-tenant model: **projects** are first-class and **API keys are
stored hashed** (only their SHA-256 is persisted; the raw key is shown once). Manage it with
the admin CLI (same tenancy DB the server uses):

```bash
python server/admin.py create-project "Team A"            # -> proj_xxxx
python server/admin.py create-key --project proj_xxxx --name ci   # -> dpk_… (save it)
python server/admin.py list-projects
python server/admin.py list-keys --project proj_xxxx
python server/admin.py revoke dpk_…
```

(For dev, `DPROV_API_KEYS="k:project,…"` switches to a static, in-memory key map.)

## The regression gate in CI

Point your app's `CloudTraceStore` at the backend so each run is recorded, then gate the
candidate against a known-good (golden) run. One command, sets the exit code:

```bash
python server/dprov_gate.py --url "$DPROV_URL" --key "$DPROV_KEY" \
    --golden "$GOLDEN_RUN_ID" --candidate "$CANDIDATE_RUN_ID"
# exit 0 = no regression · 1 = regression · 2 = error
# tune with --max-level none|low|medium|high  and  --allow-divergent
```

`dprov_gate.py` is self-contained (standard library only) — copy it straight into CI. It
prints the report summary and fails the build on regression.

## Deploy

```bash
docker compose up --build                         # http://localhost:8787/
DPROV_HOST_PORT=8788 docker compose up --build     # if 8787 is taken
```

The image is pure standard library (no `pip install`), runs with `DPROV_STORAGE=sqlite`, and
persists `/data` to a named volume (per-project trace stores + the tenancy DB). The seeded
API key is printed once in the logs (`docker compose logs`).

## Tests

```bash
python -m pytest server/tests      # 15 tests
```

Wire compatibility (ingest / query / capabilities / auth / poison-batch), the regression
gate (catches a skipped critical step, passes identical runs, lenient policy, 404), **durable
SQLite storage across a restart**, **multi-tenant auth** (create / resolve / revoke; keys
stored hashed), the **CI gate CLI** exit codes, and an **end-to-end test driving the real
`CloudTraceStore` SDK** against the server in-process.

## What's MVP vs production

Done: the SDK → ingest → query → **regression gate** → dashboard loop on the exact wire
contract; durable per-project storage; multi-tenant projects + hashed, revocable API keys; a
one-command CI gate; containerized deploy. Still ahead for a real SaaS: per-user roles &
billing, horizontal scale on a managed datastore, and a run-id index (the SQLite `get_run`
currently scans, fine at MVP volume).

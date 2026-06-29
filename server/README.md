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

Everything is generic over any consumer payload (events are stored type-erased as
`AnyTraceableEvent`), and the whole reasoning layer — query DSL, run fingerprint, semantic
alignment, the regression gate — is **reused verbatim from the library**, so the service and
the SDK can never drift.

## Run it

```bash
python server/run.py                  # http://127.0.0.1:8787  (dashboard at /)
PORT=9000 DPROV_API_KEYS="k1:teamA,k2:teamB" python server/run.py
```

Dependencies: **none beyond the standard library** + the `dprovenancekit` package. Auth is a
`DPROV_API_KEYS` map of `key:project` (defaults to `demo-key:demo`).

## The regression gate in CI

Point your app's `CloudTraceStore` at the backend so each run is recorded, then in CI fail
the build when the agent regresses against a known-good (golden) run:

```bash
curl -fsS -X POST "$DPROV_URL/api/gate" \
  -H "Authorization: Bearer $DPROV_KEY" -H "Content-Type: application/json" \
  -d "{\"golden_run_id\":\"$GOLDEN\",\"candidate_run_id\":\"$CANDIDATE\"}" \
| python -c "import sys,json; r=json.load(sys.stdin); print(r['summary']); sys.exit(0 if r['passed'] else 1)"
```

`/api/gate` accepts `max_regression_level` (`none`…`high`) and `allow_divergent_steps` to tune
strictness, and returns the full report: `passed`, `regression_level`, `fingerprint_match`,
and the `removed` / `added` / `divergent` steps.

## Tests

```bash
python -m pytest server/tests
```

11 tests: wire compatibility (ingest / query / capabilities / auth / poison-batch),
the regression gate (catches a skipped critical step, passes identical runs, lenient
policy), and an **end-to-end test driving the real `CloudTraceStore` SDK** against the
server in-process.

## MVP scope (and the path to production)

This is a working open-core MVP, deliberately minimal:

- **Storage is in-memory per project** (`InMemoryTraceStore`). Swapping in the WAL
  `SQLiteTraceStore` (or a Postgres-backed store) gives durability — the two backends are
  held at **parity** by the library's test suite, so the query/gate code is unchanged.
- **Auth is a static API-key map.** Production wants per-user keys, projects, and billing.
- **Single node, synchronous.** Fine for a pilot; horizontal scale + a managed datastore
  come next.

What it already proves: the SDK → ingest → query → **regression gate** → dashboard loop
works end to end, on the exact wire contract the library ships.

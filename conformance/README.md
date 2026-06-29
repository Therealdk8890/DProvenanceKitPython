# Conformance — one spec, many SDKs

This directory is the **canonical specification** for DProvenanceKit and the
language-neutral test fixtures that enforce it. It is the root of the
`Trace Specification → Swift / Python / Rust / TypeScript` model: every SDK implements
one spec and proves equivalence by reproducing one set of vectors.

```
conformance/
├── TRACE_SPEC_v1.md        the specification (read this first)
├── README.md               this file
├── generate_vectors.py     regenerates the vectors from the Python reference SDK
├── conformance_event.py    shared reference helpers (event, run, wire-DSL, evaluator)
└── vectors/                the frozen, language-neutral golden vectors
    ├── payload_encoding.json
    ├── run_fingerprint.json
    ├── query_semantics.json
    ├── profile_hash.json
    └── alignment_verdict.json
```

## What the vectors are

Each file is self-describing JSON: a `spec_version`, a `description`, and a list of
`cases` (some also carry an `algorithm` string or a shared `corpus`). A case pairs an
**input** with the **exact output** any conformant SDK must produce. See
[`TRACE_SPEC_v1.md`](TRACE_SPEC_v1.md) for what each file pins and §11 for the
per-SDK conformance checklist.

## Why a shared oracle

Two independent implementations that "happen to agree" drift. Pinning the behavior in
language-neutral files turns equivalence into something a CI job checks on every commit,
in every language — the fingerprint, the profile hash, query results, and alignment
verdicts cannot silently diverge.

The **Python SDK is the v1 reference oracle** (a faithful, fully-tested port of Swift).
The vectors are generated from it and then frozen in git.

## Workflows

**Check Python conformance** (runs as part of the normal suite):

```bash
python -m pytest tests/test_conformance.py
```

This reads the committed `vectors/*.json` and fails if the implementation drifts.

**Intentionally change the contract** (e.g. a new query operator):

```bash
python conformance/generate_vectors.py   # rewrites vectors/ from the current impl
git diff conformance/vectors             # review every changed expectation
```

Regeneration is deliberate and reviewable. An accidental code change that alters a
pinned behavior shows up first as a **red conformance test**, not as a silently
regenerated file.

**Add a new SDK (Rust / TypeScript / …):** implement [`TRACE_SPEC_v1.md`](TRACE_SPEC_v1.md),
load the same `vectors/*.json`, and assert your implementation reproduces each case. No
new fixtures required — the contract is already frozen.

## Conforming implementations

| SDK | Conformance harness | Status |
| --- | --- | --- |
| Python (reference oracle) | [`tests/test_conformance.py`](../tests/test_conformance.py) | ✅ all vectors |
| Swift | `ConformanceHarness/` in the Swift repo — `swift run --package-path ConformanceHarness` | ✅ all vectors |

The Swift harness vendors a copy of these `vectors/*.json` and drives the real Swift SDK
(SQLite fingerprint, query evaluator, profile-hash contract, alignment engine). Running it
is what closed the cross-language loop — and surfaced two contract refinements now folded
into v1: §2 made explicit that sorting is *required* (the Swift store now sets `.sortedKeys`),
and the alignment vectors now pin an explicit `id` per event (the canonical ordering
tiebreaks on `(sequence, id)`, so the vector must carry the ids for any SDK to reproduce it).

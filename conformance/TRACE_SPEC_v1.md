# Trace Specification v1

The canonical, language-neutral contract for DProvenanceKit. Every SDK — Swift, Python,
and future Rust / TypeScript ports — implements *this document*, and proves it by
reproducing the golden vectors in [`vectors/`](vectors/).

> **Status:** v1 (frozen). The Python SDK is the v1 reference oracle.
> **Conformance model:** the JSON files in `vectors/` are the contract. An SDK is
> conformant for a section iff it reproduces that section's vectors exactly. The Python
> SDK's claim is enforced by [`tests/test_conformance.py`](../tests/test_conformance.py);
> regenerate the vectors only via [`generate_vectors.py`](generate_vectors.py).

This spec exists so that "the Swift and Python implementations remain behaviorally
equivalent" is an *executable* guarantee, not a hope. Where two SDKs are allowed to
differ, this document says so explicitly under **Conformance Notes**.

---

## 1. Vocabulary

| Term | Meaning |
| --- | --- |
| **Event payload** | A consumer-defined value with a stable `type_identifier` (string) and a `priority` (§3). |
| **Event** | A payload wrapped in an envelope carrying run/context/engine/sequence/span lineage. |
| **Run** | An ordered set of events sharing one `run_id`, produced inside one recording scope. |
| **Context id** | A consumer-supplied label for *what* a run is about (e.g. a case id). Many runs may share one. |
| **Sequence** | A monotonic, per-run integer assigned at record time. **Authoritative** for ordering. |
| **Engine** | The named sub-component active when an event was recorded (defaults to `"Unknown"`). |
| **Edge** | A typed provenance link between two events (§9). |

`sequence` — not `timestamp` — defines causal order. Timestamps are for display and
coarse range filtering only and MAY tie under bursts.

---

## 2. Canonical payload encoding

A payload serializes to a JSON object whose **keys are sorted lexicographically**,
encoded as **UTF-8**. Decoding MUST reconstruct an equal payload (round-trip).

Vectors: [`vectors/payload_encoding.json`](vectors/payload_encoding.json).

> **Conformance Note — sorted keys, insignificant whitespace.** Sorting is **required**: a
> default encoder that preserves insertion order is *not* conformant. The Python reference
> (`json.dumps(…, sort_keys=True)`) emits `", "` and `": "` separators, e.g.
> `{"a": 1, "b": 2}`; the Swift store sets `JSONEncoder.outputFormatting = [.sortedKeys]`,
> emitting `{"a":1,"b":2}` — same keys in the same order, different separators. **These are
> byte-different but semantically identical.** v1 does **not** require byte-identical payload
> encoding across SDKs, because:
>   1. the run fingerprint (§5) does not depend on payload bytes, and
>   2. equivalence (§10) compares decoded payloads, not bytes.
>
> An SDK is conformant if it (a) sorts keys, (b) is UTF-8, and (c) round-trips. The
> `payload_encoding.json` vector pins the *reference* byte form; an SDK with compact
> separators reproduces the same logical object and is conformant. (The Swift store
> originally used a default `JSONEncoder()`, which does **not** sort; `.sortedKeys` was
> added to satisfy this section — surfaced by the Swift conformance run.) A future v2
> wanting a single byte-exact form (e.g. for content-addressing payloads) MUST mandate
> compact separators and drop this note.

---

## 3. Priority tiers

Raw integer values are part of the contract (they index per-tier buffers and drop
tallies). Ordering is meaningful: lower drops first.

| Value | Name | Drop behavior |
| --- | --- | --- |
| `0` | `TELEMETRY` | Dropped first. MUST NOT affect reasoning correctness or diff results. |
| `1` | `DIAGNOSTIC` | Qualitative debug state. |
| `2` | `STRUCTURAL` | Logic/sequence integrity. Capped per run under extreme load; preserved globally if possible. |
| `3` | `CRITICAL` | Replay/anomaly boundary. **Never dropped.** |

Load-shedding MUST be accounted by tier (a "drop tally"), never silent.

---

## 4. SQLite storage schema

`PRAGMA user_version = 2`. WAL mode, `synchronous=NORMAL`, `temp_store=MEMORY`.

```sql
CREATE TABLE runs (
    run_id      TEXT PRIMARY KEY,
    context_id  TEXT,
    start_time  INTEGER,      -- microseconds since epoch
    end_time    INTEGER,      -- microseconds since epoch
    event_count INTEGER,
    fingerprint TEXT          -- §5
);

CREATE TABLE trace_events (
    id             TEXT PRIMARY KEY,   -- event uuid
    run_id         TEXT NOT NULL,
    context_id     TEXT NOT NULL,
    priority       INTEGER NOT NULL,   -- §3
    sequence       INTEGER NOT NULL,   -- authoritative order
    engine         TEXT,
    span_id        TEXT,
    parent_span_id TEXT,
    type           TEXT NOT NULL,      -- payload type_identifier
    payload        BLOB NOT NULL,      -- §2 canonical bytes
    timestamp      INTEGER NOT NULL    -- microseconds since epoch
);

CREATE TABLE trace_edges (
    source_id  TEXT NOT NULL,
    target_id  TEXT NOT NULL,
    edge_type  TEXT NOT NULL           -- §9
);
```

Indices: `trace_events(run_id)`, `(type)`, `(run_id, type)`, `(timestamp)`,
`(run_id, sequence)`, `(priority)`; `trace_edges(source_id, edge_type)` and
`(target_id, edge_type)`.

Timestamps are stored in **microseconds** (`floor(seconds * 1_000_000)`).

---

## 5. Run fingerprint — the equivalence anchor

The fingerprint is a run's structural identity. It is the **primary** cross-language
equivalence check: two runs with the same fingerprint took the same typed steps through
the same engines in the same order.

```
signature(event) = type + ":" + engine + "|"          # engine = "" only if null/empty
fingerprint(run) = sha1( concat( signature(e) for e in run, in commit order ) ).hexdigest()
```

- **Commit order** equals record (sequence) order under non-lossy conditions.
- The fingerprint is **payload-independent** (see §2's note) and **order-sensitive**.

Vectors: [`vectors/run_fingerprint.json`](vectors/run_fingerprint.json).

---

## 6. Query language

One AST, evaluated identically by every backend. The fluent DSL lowers to these nodes;
the **wire form** (below) is the canonical serialization a client sends to a remote
backend.

| Node | Wire form | Semantics |
| --- | --- | --- |
| And | `{"type":"and","nodes":[…]}` | All children match. Empty ⇒ all runs. |
| Or | `{"type":"or","nodes":[…]}` | Any child matches. Empty ⇒ all runs. |
| Not | `{"type":"not","node":…}` | Child does not match. |
| ContextIDEquals | `{"type":"contextIDEquals","id":S}` | Run's `context_id == S`. |
| EngineNameEquals | `{"type":"engineNameEquals","name":S}` | Some event has `engine == S`. |
| ContainsStep | `{"type":"containsStep","step":S}` | Some event has `type == S`. |
| MissingStep | `{"type":"missingStep","step":S}` | No event has `type == S`. |
| Sequence | `{"type":"sequence","steps":[…]}` | The steps appear as an ordered subsequence (by `sequence`). |
| After | `{"type":"after","step":S,"followedBy":T}` | `T` occurs at or after the **first** `S`. |
| Before | `{"type":"before","step":S,"precededBy":T}` | `T` occurs strictly before the **first** `S`. |

**Ordering rule.** All temporal operators order by `sequence`, never `timestamp`.
`after`/`before` anchor to the **first** occurrence of `step` (`MIN(sequence)`).

**Parity requirement.** An in-memory evaluator and a SQL-compiled backend MUST return
the identical set of runs for every query. v1 pins both backends against one corpus.

Vectors: [`vectors/query_semantics.json`](vectors/query_semantics.json) — a corpus plus
queries in wire form, each with the exact set of matching `context_id`s.

---

## 7. Cloud wire format

**Ingest** (`POST /ingest`, `Authorization: Bearer <key>`): a JSON array of event
objects:

```json
{ "id": "...", "run_id": "...", "context_id": "...", "priority": 3, "sequence": 0,
  "engine": "Planner", "span_id": null, "parent_span_id": null,
  "type": "finalDecisionMade", "payload": { /* decoded §2 object */ },
  "timestamp": 1719446400000000 }
```

`payload` is the decoded JSON object when decodable, else a base64 string of the raw
bytes. `timestamp` is microseconds (§4).

**Query** (`POST /query`): `{"schemaVersion": "1.0", "dsl": <wire node §6>, "limit": N}`.
A backend that cannot serve a schema returns `400/422` with
`{"error":"UNSUPPORTED_SCHEMA","expected":…,"received":…}`; unimplemented ⇒ `501`.

---

## 8. Recording model

- `record` is synchronous and non-blocking: it touches only an in-memory buffer and
  returns. The event is observable to a same-thread `flush` (happens-before).
- `flush` is a true barrier.
- Ambient run / engine / span context propagates implicitly (Python `contextvars`,
  Swift task-locals). `sequence` is assigned under a per-run lock at record time.
- Self-referential edges (`source == target`) are rejected at the write boundary.

---

## 9. Provenance edges

`edge_type` is one of: `derivedFrom`, `influencedBy`, `generatedFrom`, `verifiedBy`,
`correctedBy`, `informed`. **Lineage** of an event = the transitive closure of incoming
edges; **impact** = the transitive closure of outgoing edges.

---

## 10. Alignment

Decides whether two runs are behaviorally equivalent under a configured profile.

### 10.1 Profile hash

Two runs are only comparable under the *same* profile. The profile hash is that
version-stamp, and is a hard cross-language contract.

```
payload =
  "contractVersion:" + CONTRACT_VERSION + "\n" +     # "1.0.0"
  "engineVersion:"   + engineVersion    + "\n" +
  "strategy:"        + strategy         + "\n" +
  "profileVersion:"  + version          + "\n" +
  "typeWeight:"            + fmt(typeWeight)            + "\n" +
  "payloadWeight:"         + fmt(payloadWeight)         + "\n" +
  "structuralWeight:"      + fmt(structuralWeight)      + "\n" +
  "temporalWeight:"        + fmt(temporalWeight)        + "\n" +
  "semanticThreshold:"     + fmt(semanticThreshold)     + "\n" +
  "maxAmbiguousCandidates:" + maxAmbiguousCandidates    + "\n" +
  "ambiguityDeltaThreshold:" + fmt(ambiguityDeltaThreshold) + "\n" +
  "alignmentMode:"   + alignmentMode    + "\n" +
  "evaluatorIdentifier:" + evaluatorIdentifier          # no trailing newline
profile_hash = sha256( utf8(payload) ).hexdigest()
```

**`fmt(x)` (float formatting):** if `x` is integral, render with exactly one decimal
(`0.0`, `1.0`); otherwise render the shortest round-tripping decimal (`0.95`, `0.15`).
This mirrors Swift's default `Double` interpolation and is a conformance-critical detail.

Vectors: [`vectors/profile_hash.json`](vectors/profile_hash.json).

### 10.2 Verdict

`align(base, comparison, minimum_priority)` (default `minimum_priority = STRUCTURAL`,
i.e. telemetry/diagnostic events are excluded) returns:

- a **regression risk**: `level ∈ {none, low, medium, high}` with a `strength` in 0..1;
- per-event **alignment states**, each one of: `exactMatch`, `semanticMatch`,
  `reordered`, `ambiguous`, `added`, `removed`.

Degrading a CRITICAL step is the high-severity signal: a critical step that is **removed**
(`high`, strength `0.95`) or **reordered** (`high`, strength `1.0`, since reordering a
critical step can invert a dependency) drives the regression level. Reordering of
non-critical (structural/diagnostic) steps stays `none`. The exact verdicts are pinned by the
vectors below; an SDK reproduces them rather than re-deriving the thresholds.

The canonical alignment ordering sorts by `(sequence, id)` — where, for a matched/removed
alignment the key is its **base** event's `(sequence, id)`, and for an added alignment its
**comparison** event's. The `id` tiebreak only bites when two alignments share a sequence
(e.g. a matched step and an added step both at sequence *n*); there the lexicographic order
of the event UUIDs decides. Because UUIDs are otherwise free, **the alignment vectors pin an
explicit `id` on every base/comparison event** so the ordering is reproducible by any SDK: a
conforming harness MUST build its runs with the ids carried in the vector (not freshly
generated ones), or the tied cases will order differently. (Surfaced by the Swift conformance
run, which initially mis-ordered an added step until it adopted the vector's ids.)

**Canonical conformance evaluator** (`ExactEquality_v1`): `similarity(a, b) = 1.0` iff
the decoded payloads are fully equal, else `0.0`. This makes alignment vectors
language-neutral (no fuzzy scoring to reproduce).

Vectors: [`vectors/alignment_verdict.json`](vectors/alignment_verdict.json) — base +
comparison runs (each event carrying an explicit `id`) with the resulting level and ordered
state kinds.

---

## 11. Conformance checklist for a new SDK

A Rust / TypeScript / … SDK is v1-conformant when, reading the same `vectors/*.json`:

1. **payload_encoding** — sorts keys, UTF-8, round-trips (byte-exactness optional, §2).
2. **run_fingerprint** — reproduces every SHA-1 exactly.
3. **query_semantics** — every backend returns the exact `expected_context_ids`.
4. **profile_hash** — reproduces every SHA-256 exactly (mind `fmt`).
5. **alignment_verdict** — reproduces level + ordered state kinds under `ExactEquality_v1`.

Vectors are versioned by `spec_version`. A backward-incompatible change ships as
Trace Specification **v2** with its own vector set; v1 vectors remain frozen.

# git-blob-rewrite

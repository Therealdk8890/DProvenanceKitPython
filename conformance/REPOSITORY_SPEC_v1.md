# Reasoning Repository Contract v1

**Status: opt-in v1 candidate.** This contract is independently versioned from
frozen Trace Specification v1 and `DPK-BINARY-V1`. Existing trace fingerprints,
databases, attestations, APIs, and alignment profiles retain their meanings.

The repository versions explicitly captured workflow assets and executions. It
does not reveal hidden model thoughts or establish the factual truth of events.

## Canonical objects

An object is the concatenation of a length-prefixed UTF-8 string
`DPK-OBJECT-V1`, a length-prefixed type string, unsigned big-endian u64 schema
version `1`, and one recursively tagged value. String/byte lengths and container
counts are unsigned big-endian u64. Object IDs are lowercase hex SHA-256 of the
complete canonical bytes, including the domain/type/version.

| Tag byte | Value and following bytes |
|---|---|
| 0 | null |
| 1 | false |
| 2 | true |
| 3 | signed big-endian i64 |
| 4 | u64 UTF-8 byte count, then string bytes |
| 5 | u64 byte count, then opaque bytes |
| 6 | u64 count, then tagged array values in order |
| 7 | u64 count, then untagged length-prefixed string key and tagged value pairs |

Map keys are unique and sorted by unsigned UTF-8 byte order. Unicode is never
normalized: composed and decomposed keys can coexist. Floats, invalid UTF-8,
unsupported types/versions/tags, duplicate or unsorted keys, trailing bytes, and
out-of-range integers are rejected. Limits are 64 MiB/object, 64 nested levels,
and one million decoded values/items. Timestamps use integer microseconds;
model decimal configuration can use explicit decimal strings. Raw payloads are
byte blobs and are never re-encoded for identity or verification.

All six object types carry a map payload with these exact top-level fields:

| Type | Payload |
|---|---|
| `blob` | `data`: exact bytes |
| `workflow` | `assets`: map of logical name to blob ID; `config`, `source`, `capture_policy`: maps |
| `run` | `run_id`, `context_id`, `workflow`, `workflow_binding`, `events`, `edges`, `capture_quality`, `graph_health` |
| `commit` | `parents`: zero or one commit ID; `workflow`, `run`, `message`, `author`, `created_at_us` |
| `evaluation` | `base`, `candidate`: commit IDs; `policy`, `policy_digest`, `report` |
| `signature` | `subject`: commit ID; `algorithm`, `key_id`, `public_key`, `signature` |

Run events contain `id`, `run_id`, `context_id`, `engine`, `schema_version`,
`sequence`, nullable `span_id`/`parent_span_id`, `type`, integer `priority`,
`payload` blob ID, `timestamp_us`, and `schema_provenance` (`recorded` or
`legacy_assumed_v1`). Edges contain string `source_id`, `target_id`, `type`.
The edge vocabulary is unchanged from Trace Specification v1.

`capture_quality` contains lifecycle (`completed`, `failed`, `interrupted`, or
`legacy_unknown`), nullable known attempt counts `attempted_events` and
`attempted_edges`, integer `dropped_events`, `dropped_edges`, `undecoded_events`,
`schema_provenance`, boolean `sealed`, and optional captured `workflow_id`.
`graph_health` contains `valid`, sorted `reasons`, `event_count`, `edge_count`;
verifiers recompute it from the event/edge data. Sequence gaps, duplicates,
mixed-run events, missing/inconsistent span parents, span/provenance cycles,
self edges, duplicate edges, and external/missing edge endpoints are surfaced.
Incomplete or malformed captures can be archived but cannot pass the strict gate.

A `prebound` run requires both a persisted repository binding and the matching
workflow ID recorded in the capture store before any event/edge attempt. An
explicit workflow supplied after execution is `post_hoc`; completeness stays
unproven. Legacy imports retain unknown quality rather than acquiring successful
capture provenance merely by being imported.

## History and evaluation

Repository SQLite is separate from the live trace database. Objects are inserted
immutably. Branch/tag/baseline refs and the local reflog update transactionally;
conditional updates compare the exact previous object ID. Ref namespaces are
`refs/heads`, `refs/tags`, and `refs/baselines`. Tags are create-only. Switching
branches changes the selected history ref and never checks out source files.
The first commit has no parent; merge/rebase/history pruning are outside v1.

Each commit binds exactly one workflow and one run whose workflow references
match. Evaluations and signatures are detached to avoid identity cycles.
Author strings are producer claims, not authenticated identities. Content hashes
and the reflog do not establish an independently witnessed append-only history or
detect replacement with an earlier internally valid repository root.

The built-in policy is `repository-strict-v1`, using the released
`TraceAlignmentEngine/1.0.0` and `strict_audit_v1`/exact-equality regression gate,
with independent span hierarchy, order, and provenance edge gates. It does not
select experimental Structural Alignment v1. Policy fields also pin normalizer
`json-value-v1`, minimum priority (default 2), and comparison event budget
(default 1,000; configurable up to 10,000). No recorded evaluator code is imported.

`json-value-v1` parses strict UTF-8 JSON with unique keys and finite exact decimal
numbers. Keys sort by UTF-8 bytes; insignificant whitespace and decimal formatting
do not affect comparison. Numeric exponent magnitude is bounded to 1,000 and
nesting to 64. Volatile envelope UUIDs/timestamps are excluded from comparison,
while exact archival blob bytes remain unchanged. Optional `dpk.logical_step_id`
payload annotation participates in event identity. Unsupported payload comparison
does not silently pass.

Reports distinguish `integrity_valid`, `capture_complete`, `policy_accepted`, and
outcomes `accepted`, `regressed`, `incomplete`, `invalid`. Empty evidence, unknown
or post-hoc binding, failed/interrupted capture, any counted loss/undecoded event,
and an exceeded comparison budget do not produce acceptance. Workflow asset and
metadata changes appear separately from execution findings and do not themselves
fail an otherwise equivalent path. Policy digests hash canonical sorted compact
UTF-8 JSON policy bytes. An evaluation binds exact input commit IDs, policy, and
report. Promotion recomputes acceptance and requires CAS against the evaluated
baseline tip. Initial baseline creation requires an accepted self-comparison and
an absent baseline ref.

## Portable bundles and signatures

Bundles use a strict, unencrypted ZIP_STORED subset: no ZIP64, compression,
comments, extras, data descriptors, spanning, leading/trailing bytes, duplicate
paths, or arbitrary extraction. Limits are 256 MiB and 10,000 objects. Entries are
exactly `manifest.json` and `objects/<64-lowercase-hex>.bin`.

The manifest is sorted compact UTF-8 JSON with fields `format` =
`DPK-REPOSITORY-BUNDLE-V1`, integer `version` = 1, unique sorted `roots` and
`objects` ID arrays, and `refs` mapping full ref names to included commit IDs.
Every typed reference must resolve inside the bundle. The inventory is the full
root closure, plus detached signatures attached to reachable commits; unrelated
inventory is rejected. Evaluations can be selected as additional roots.

Import validates all bytes, schemas, typed references and available signature
integrity before one SQLite transaction. It runs no hooks, source checkout,
package installs, or evaluators. No refs change unless the caller explicitly maps
imported refs to new local names; existing refs are never overwritten and baseline
refs cannot be promoted through import. Export reads a consistent SQLite snapshot.

Optional detached P-256 signatures use algorithm `P256-SHA256`, uncompressed
X9.63 public-key bytes, ASN.1 DER ECDSA signatures, and key ID = lowercase hex
SHA-256(public-key bytes). The signed message is ASCII
`DPK-REPOSITORY-SIGNATURE-V1`, one NUL byte, then the raw 32-byte subject commit
digest. The signature is outside the subject ID. Signed imports require the
optional crypto extra; invalid signatures fail import. Embedded-key integrity is
distinct from signer trust established by independently provisioned key IDs.

## Conformance

`vectors/repository_v1.json` freezes eight canonical object encodings/IDs,
including exact Unicode key distinctions and integer bounds, plus complete
unsigned and signed bundles. Binary copies live under `fixtures/`.
The signing fixture uses the deliberately public test scalar 1; it is not a key
custody example. `generate_repository_vectors.py` is an explicit regeneration
tool; existing vector families must not be rewritten for this protocol.

Python tests cover actual recording/sealing, fresh-equivalent and missing-verify
runs, workflow diffs, reopened read-only access, ref races, promotion, incomplete
quality, bundle attack rejection/atomicity, signatures, and exact canonical IDs.
Independent Swift reader tests consume the same bytes; runtime Swift validation
requires an Apple/Swift development environment.

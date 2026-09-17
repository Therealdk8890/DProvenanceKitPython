# Cross-language conformance gaps

Honest inventory of what Trace Spec v1 vectors pin today, what is deliberately
Swift-only, and what is deferred. Buyers and engineers should treat this as the
authoritative gap list — not the product README.

## Pinned by shared vectors (both SDKs must reproduce)

| Vector | Contract |
| --- | --- |
| `payload_encoding.json` | Sorted-key UTF-8 payload encoding; round-trip. Byte-identical separators are **not** required (§2). |
| `run_fingerprint.json` | SHA-1 over `(type, engine)` signatures in commit order. |
| `query_semantics.json` | Wire DSL → exact matching `context_id`s (in-memory + SQLite). |
| `profile_hash.json` | SHA-256 profile execution-contract stamp (float `fmt` rules). |
| `alignment_verdict.json` | Regression level + ordered state kinds under `ExactEquality_v1`. |

Both harnesses fail closed on this corpus. The Python suite is the reference
oracle; the Swift `ConformanceHarness` vendors the same JSON and
`.github/workflows/vector-sync.yml` fails on drift.

## Attestation (DPK-BINARY-V1) — implemented on both (MVP)

| Surface | Status |
| --- | --- |
| Canonical `DPK-BINARY-V1` encoding + SHA-256 trace digest | **Both** — Python `dprovenancekit.attestation` matches Swift `TraceAttestationCanonicalizer`. |
| ECDSA P-256 DER sign / verify (`P256-SHA256`) | **Both** — Python software keys via optional `dprovenancekit[crypto]` (`cryptography`); Swift CryptoKit software + Secure Enclave. |
| Golden vector `attestation-v1.json` | **Shared** — sourced from Swift `docs/test-vectors/attestation-v1.json`; vendored at `conformance/vectors/attestation-v1.json` and `tests/fixtures/attestation-v1.json`. Python verifies the Swift-produced fixture fail-closed. |
| CLI | Swift `dpk verify` / `attest-demo`; Python `dpk attest sign\|verify`. |

## Swift-only (not faked in Python)

| Surface | Location | Status |
| --- | --- | --- |
| **Proof packs** | `docs/test-vectors/proof-pack-v*.json` + `docs/PROOF_PACK.md` | **Swift-only.** No Python port in this MVP. |
| Secure Enclave key types | Swift `SecureEnclaveTraceAttestationKey` | Apple-platform only. |
| Keychain custody recipes | `docs/ATTESTATION.md` | Application-side; not a library API on either SDK. |
| SQLite `saveAttestation` / `verifyStoredAttestation` persistence helpers | Swift store | Python MVP signs/verifies portable JSON documents only. |

## Deferred (intentionally not vectorized yet)

| Gap | Why deferred |
| --- | --- |
| **Fuzzy / graded equivalence evaluators** | Conformance oracle is `ExactEquality_v1` only. Fuzzy scoring is language-sensitive and would break the neutral corpus. Adversarial suites cover greedy traps separately in each language. |
| **Load-shedding / drop tallies** | Behavioral parity exists in unit tests; not part of Trace Spec v1 golden vectors. |
| **Undecoded / quarantined cloud payloads** | Swift cloud store surfaces `undecodedEventCount`; Python cloud path differs. Cross-lang vectorization waits on a shared wire contract. |
| **Production matcher rewrite** | Out of scope — expand vectors/harnesses, do not rewrite greedy matchers. |
| **Proof-pack cross-language vectors** | Wait until a Python proof-pack implementation exists. |

## Next gaps (priority order)

1. Proof packs in Python (or documented permanent Swift-only boundary).
2. Optional Trace Spec v1.1 vector file for shed tallies (if buyers need drop accounting pinned).
3. Documented fuzzy-evaluator profile identifiers (still ExactEquality for the golden corpus).

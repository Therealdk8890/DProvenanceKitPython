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

## Swift-only (not faked in Python)

| Surface | Location | Status |
| --- | --- | --- |
| **Crypto attestation / proof packs** | `docs/test-vectors/attestation-v1.json`, `proof-pack-v1.json`, `proof-pack-v2.json` + `docs/ATTESTATION.md` | **Swift-only.** Signing / Secure Enclave / Keychain custody has no Python port yet. Do **not** invent Python attestation vectors until a real signer exists. |
| Secure Enclave key types | Swift `SecureEnclaveTraceAttestationKey` | Apple-platform only. |

Tracked follow-up: **Python → signed artifacts** (software P-256 signer + shared
attestation vectors). Until then, attestation is explicitly out of Trace Spec v1
cross-language scope.

## Deferred (intentionally not vectorized yet)

| Gap | Why deferred |
| --- | --- |
| **Fuzzy / graded equivalence evaluators** | Conformance oracle is `ExactEquality_v1` only. Fuzzy scoring is language-sensitive and would break the neutral corpus. Adversarial suites cover greedy traps separately in each language. |
| **Load-shedding / drop tallies** | Behavioral parity exists in unit tests; not part of Trace Spec v1 golden vectors. |
| **Undecoded / quarantined cloud payloads** | Swift cloud store surfaces `undecodedEventCount`; Python cloud path differs. Cross-lang vectorization waits on a shared wire contract. |
| **Production matcher rewrite** | Out of scope — expand vectors/harnesses, do not rewrite greedy matchers. |

## Next gaps (priority order)

1. Software P-256 attestation in Python + shared attestation vectors (replace Swift-only status).
2. Optional Trace Spec v1.1 vector file for shed tallies (if buyers need drop accounting pinned).
3. Documented fuzzy-evaluator profile identifiers (still ExactEquality for the golden corpus).

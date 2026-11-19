# Event chain v1 — isolated Python prototype

**Experimental review only.** The original private §10 draft was not recovered, so the candidate preimage layout is not normative. This package does not change either released SDK or the frozen Trace Specification v1. The `experimental/` namespace is intentionally outside the SDK's setuptools discovery pattern.

## Run the gate

```bash
python -m pip install -r event_chain_gate_requirements.txt
python scripts/event_chain_gate.py
```

The gate runs Ruff (same E/F baseline as the existing Python SDK), strict mypy on all prototype runtime code targeting Python 3.9, and 50 pytest cases. Every stage runs even when one fails; any nonzero stage causes the aggregate gate to return 1. CI configuration also runs tests on real Python 3.9. The existing full SDK quality and regression gates must stay in place. On GitHub, configure the candidate checks and existing lint + type-check/test checks as required branch-protection checks before merge; a workflow file alone cannot enforce that policy.

## Verified locally

- 50 pytest tests passed on Python 3.13.
- All six positive candidate fixtures reproduced, including exact preimages and expected entry hashes.
- Python 3.9 grammar parse succeeded; not a Python 3.9 runtime test.
- Gate fail-closed behavior exercised: Ruff and mypy were unavailable and package installation failed in this environment. The aggregate gate returned 1. Neither lint nor strict mypy is claimed green.

## Deliberate limitations

- This is an in-memory prototype, not production or an independent security validation. No raw trace adapters, untrusted wire importer, resource caps, SQLite mutation, signed closure, or Swift implementation. It accepts already constructed typed records and does not parse an untrusted chain transport. The initial JSON grammar validation and Unicode edge cases still need parity review; exact payload bytes are preserved. Stated chain invariants do not promise capture completeness.
- `ChainBuilder` validates imported chains once, then maintains an in-memory UUID set, head, last sequence and entry list. Incremental append is amortized O(1) in entry count, plus encoding/hashing input bytes; `snapshot` is O(n) and full `verify` remains O(n) plus byte sizes. Stateless `append` still validates the supplied chain and copies it, deliberately. The writer is single-owner, not thread-safe or persistent; do not place it on the synchronous recorder path. A deterministic 1,024-event test asserts exactly 1,025 SHA-256 operations, including genesis; imports rehash the prefix once only. No wire/preimage change.
- Expected count and head arguments are externally supplied commitments, not signature verification. A rewritten chain may remain internally consistent. Missing/invalid signatures, rollback/freshness, trace-wide edge binding, and capture losses are unresolved closure work.
- Candidate error precedence and raw-to-attestable normalization are provisional. No normative fixture freeze or production merge until the original draft is reconciled, resource bounds agreed, the independent Swift implementation reproduces the fixtures, and existing and new quality gates pass.
- No repository files, CI rules, releases, or PRs were changed by this package. Direct git access failed DNS resolution; the connector commit API does not provide an explicit author identity argument. Use the review patch on a fresh branch, with the configured DCO identity, rather than accepting unverified commit attribution. The workflow is a proposed addition, not a running GitHub check.

## Review patch integration

The companion Git patch adds the candidate files without replacing the SDK README or existing CI workflow. Apply on a new branch with `git apply --check` first; inspect the diff and run the aggregate gate before signing/committing. The candidate workflow is additive and does not weaken existing CI. Keep the PR draft until the gate passes and the specification is reconciled.

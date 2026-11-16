# DProvenanceKit 0.10.0 release preparation

Status: **unpublished candidate**. Latest published Python and Swift versions: **0.9.2**.
Python tag: `v0.10.0`. Swift tag: `0.10.0`. The unpublished 0.9.3 preparation is
superseded; its workbench changes are included in 0.10.0. Numbered changelog entries
and build-version constants describe the prepared source, not proof of publication.

## Release scope

| Area | Python | Swift |
|---|---|---|
| Local reasoning history | Opt-in capture, immutable commits, conditional refs, comparisons, promotion and CLI | No local repository operations |
| Portable evidence | Canonical objects, full-closure bundles, P-256 signing and verification | Canonical objects and bounded, read-only bundle/signature verification |
| Workbench | Read-only multi-database trace viewer and paged repository-history viewer | Existing Apple UI; no Python web workbench |
| Structural Alignment v1 | Experimental reference engine | Experimental engine using shared fixtures |
| Configuration authorization | Experimental fingerprint checks and trusted Ed25519 manifest verification; signature operations need `[crypto]` | Experimental CryptoKit implementation |
| Attestation hardening | External P-256 signing-provider protocol and shared malformed-wire cases | Shared malformed-wire cases and stricter macOS RFC 3161 signer-certificate checks |

Existing trace commands and frozen Trace Specification v1/DPK-BINARY-V1 formats stay
available. The experimental alignment engine does not replace the established gate.
The order-intent proposal is documentation only. A signing-provider protocol is not a
delivered HSM/TPM integration or hardware-attestation verifier. Matching fixture results
establish conformance to those fixtures, not identical SDK capabilities or field accuracy.

Repository CLI branch commits require the documented expected parent; trusted verification
requires a trust policy supplied independently of the artifact. Capture metadata records
instrumented activity and loss information, not proof of complete observation or truth.
Swift's RFC 3161 certificate checks can reject formerly accepted tokens; CMS authentication
remains macOS-only with pinned-TSA trust, not general PKI-chain validation.

## Python validation

Run against the exact commit intended for publication in an isolated environment:

```bash
python -m pip install -e ".[dev]" build twine ruff mypy
python scripts/check_release_version.py v0.10.0
python scripts/check_version_surface.py
python -m pytest -rs
ruff check dprovenancekit/
mypy dprovenancekit/
python examples/reliability_gate_benchmark.py --fail-on-missed-regression
python -m build
python -m twine check dist/dprovenancekit-0.10.0*
```

Install the wheel into a fresh virtual environment outside the source checkout.
Verify metadata reports exactly 0.10.0, core dependencies remain empty, the packaged
`index.html`/`repository.html` resources exist, CLI help works and both offline demos run.
The repository demo must fail its skipped-verification experiment and pass its fresh
matching run, then reproduce the evaluation after bundle export/import. Also verify
the sdist builds the same versioned wheel.

Require all Python-version and OS jobs, repository-protocol jobs, installed-wheel
smoke tests, lint/type checks and Docker/Compose checks on the candidate PR. Heavy
real-framework integrations run only on schedule/manual dispatch: a skipped job is
not evidence of a successful integration test. Record its tested SHA if run separately.

## Swift and cross-language validation

Follow the [Swift release checklist](https://github.com/Therealdk8890/DProvenanceKit/blob/release/0.10.0-preparation/docs/RELEASE_0.10.0.md).
Compare every vendored Swift vector byte-for-byte with Python `conformance/vectors/`,
and compare the shared malformed-attestation fixture. Run both production engines'
shared-fixture tests; matching file bytes alone are not an engine-execution result.

## Publication gates

- [ ] Record both final candidate SHAs, review the diff and resolve CI/DCO failures.
- [ ] Both SDKs' full CI and release-specific checks pass at those SHAs; record skips.
- [ ] Merge both preparation PRs, then verify the final main commits and their checks.
- [ ] Confirm `v0.10.0` and `0.10.0` do not already exist; never reuse or move a release tag.
- [ ] Publish Python `v0.10.0` using GitHub Release and the Trusted Publishing workflow.
- [ ] Confirm the release-gate tests, build, metadata checks, attestations and PyPI upload succeed.
- [ ] Install exactly `dprovenancekit==0.10.0` from PyPI in a fresh environment and repeat the wheel smoke checks; verify artifact provenance.
- [ ] Publish Swift `0.10.0` and verify a clean consumer resolves that tag and its supported library targets build.
- [ ] Update latest-published status, candidate install guidance and changelog dates only after the corresponding publication is verified.
- [ ] After PyPI verification, update bundled/downstream Action package pins to the published 0.10.0 artifact; their current 0.9.2 pins intentionally target an available package.
- [ ] Announce a coordinated release only after both distribution paths are confirmed.

Release preparation does not create tags, publish packages or satisfy these gates by
itself. Report per-SDK status if either publication fails.

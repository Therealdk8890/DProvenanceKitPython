# Releasing `dprovenancekit` to PyPI

The library is the free, Apache 2.0-licensed client SDK. The hosted backend lives in a separate
private repo and is **not** packaged here — only the `dprovenancekit/` package is published.

## Coordinated DProvenanceKit product versions

Python and Swift share one product release version, while retaining independent packages,
APIs, capabilities, and release gates. Matching numbers do not imply feature parity.

For a coordinated release, prepare both SDKs at the same version, require each repository's
full CI and release-specific checks to pass, then publish through each repository's own
process. Python uses its `vX.Y.Z` tag and PyPI Trusted Publishing workflow; Swift uses its
Swift package release tag and GitHub Release. Announce the product release as complete only
after both publication paths are confirmed. If either path fails, report each SDK's status
separately. Never reuse or rewrite a published or failed tag.

The coordinated 0.9.2 Python and Swift releases have been published. The next coordinated product release is 0.9.3; Python release preparation includes the multi-database workbench, while Swift must independently complete its own release checks and publish the matching 0.9.3 tag. Do not describe the product release as coordinated until both publication paths are confirmed.

## One command (manual)

```bash
python -m build                 # -> dist/*.whl + dist/*.tar.gz
python -m twine check dist/*    # validate metadata + README rendering
python -m twine upload dist/*   # needs a PyPI API token (or use the workflow below)
```

The build is reproducible and dependency-light; verified locally — `twine check` passes and
a clean `pip install dist/*.whl` imports the package, its integrations, and the
`dprovenancekit` console script.

> **Note:** a manual `twine upload` produces an *unattested* release — no PEP 740 attestations
> on PyPI and no GitHub artifact attestation, so `gh attestation verify` will fail for its
> files. Use the workflow below for anything users are expected to verify (see
> [Build provenance](#build-provenance-attestations)).

## Automated (recommended)

Publishing on a GitHub Release is wired up in
[`.github/workflows/publish-pypi.yml`](.github/workflows/publish-pypi.yml) via **PyPI Trusted
Publishing** (OIDC — no stored token). One-time PyPI setup: add a trusted publisher for the
project (owner/repo, workflow `publish-pypi.yml`, environment `pypi`). Then:

1. Bump `version` in `pyproject.toml` **and** the `__version__` fallback in
   `dprovenancekit/__init__.py` (installed copies read the version from package
   metadata; the fallback only applies when running from a source checkout, but
   it must not drift).
2. Run `python scripts/check_release_version.py vX.Y.Z` with the exact tag you intend
   to publish. The release workflow repeats this check automatically before building.
3. Commit, create a new tag (never reuse a published or failed release tag), push it,
   and publish the matching GitHub Release.
4. The workflow verifies the tag/version match, runs the release test gate, builds,
   `twine check`s, attests, and uploads.

## Build provenance (attestations)

Releases cut through the publish workflow ship with verifiable build provenance — fitting,
for a provenance toolkit. Two independent layers, both produced automatically (PEP 740
attestations exist for all published releases; GitHub artifact attestations start with the
first release after v0.6.0):

1. **PyPI attestations (PEP 740).** `pypa/gh-action-pypi-publish` generates Sigstore-signed
   publish attestations by default under trusted publishing and uploads them alongside the
   files. Check the per-file provenance entries on
   [pypi.org/project/dprovenancekit/#files](https://pypi.org/project/dprovenancekit/#files).
2. **GitHub artifact attestations (SLSA build provenance).** The workflow runs
   `actions/attest-build-provenance` on `dist/*`, recording in GitHub's attestation store
   which workflow, commit, and runner produced each artifact. Verify a downloaded file with:

   ```bash
   gh attestation verify dprovenancekit-<version>-py3-none-any.whl \
     --repo Therealdk8890/DProvenanceKitPython
   ```

Neither layer needs a stored key or manual step; both derive from the workflow's OIDC identity.

## Install (once published)

```bash
pip install dprovenancekit
pip install "dprovenancekit[langchain]"        # + LangChain adapter
pip install "dprovenancekit[openai-agents]"    # + OpenAI Agents adapter
```


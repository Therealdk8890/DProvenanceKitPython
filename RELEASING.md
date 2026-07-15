# Releasing `dprovenancekit` to PyPI

The library is the free, Apache 2.0-licensed client SDK. The hosted backend lives in a separate
private repo and is **not** packaged here — only the `dprovenancekit/` package is published.

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
2. Commit, tag (`git tag v0.1.0 && git push --tags`), and publish a GitHub Release.
3. The workflow builds, `twine check`s, attests, and uploads.

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


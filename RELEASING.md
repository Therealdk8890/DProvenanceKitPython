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
3. The workflow builds, `twine check`s, and uploads.

## Install (once published)

```bash
pip install dprovenancekit
pip install "dprovenancekit[langchain]"        # + LangChain adapter
pip install "dprovenancekit[openai-agents]"    # + OpenAI Agents adapter
```


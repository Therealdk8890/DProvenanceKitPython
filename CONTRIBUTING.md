# Contributing to DProvenanceKit

Thanks for your interest in improving DProvenanceKit. This is the free, Apache-2.0 client SDK;
the hosted backend lives in a separate private repository and is not developed here.

## Development setup

Requires Python 3.9+. From a checkout:

```bash
python -m pip install -e ".[dev]"
```

The core package has **zero third-party runtime dependencies** — everything under
`dprovenancekit/` (excluding the optional `integrations/` adapters) must import only the Python
standard library. Framework adapters live behind optional extras (`.[langchain]`,
`.[openai-agents]`, …) and must degrade gracefully when their framework is absent.

## Before you open a pull request

Run the same checks CI runs:

```bash
python -m pytest -q          # full test suite (currently ~430 tests, a few seconds)
python -m ruff check dprovenancekit/
python -m mypy dprovenancekit/
```

All three must pass. If your change adds behavior, add a behavioral test for it — the suite
favors driving real code paths (real threads, real HTTP servers, callbacks driven directly) over
shallow assertions.

Heavy real-framework integration tests (OpenAI Agents, LlamaIndex, CrewAI) are guarded by
`importorskip` and run on a weekly CI job, not on every PR. Adapter changes should also add
dependency-free tests that drive the adapter's handlers with stand-in objects so they run on PRs.

## Conventions

- **Docstrings state real invariants,** not restatements of the code (e.g. thread-safety caveats,
  ordering guarantees, exit-code contracts).
- **The conformance vectors under `conformance/` pin cross-language behavior.** Any change to
  alignment scoring, canonical ordering, or the profile-hash recipe must be behavior-preserving or
  accompanied by an intentional re-freeze via `conformance/generate_vectors.py`.
- **Match the surrounding style.** No drive-by refactors outside the scope of your change.
- Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/)
  (`fix:`, `feat:`, `docs:`, …).

## Adding a changelog entry

User-facing changes get an entry under `## [Unreleased]` in [CHANGELOG.md](CHANGELOG.md), grouped
under Added / Changed / Fixed / Security.

## Releasing

Maintainer process is documented in [RELEASING.md](RELEASING.md).

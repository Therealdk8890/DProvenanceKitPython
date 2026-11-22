# DProvenanceKit Python 0.11.0 release checklist

Status: **prepared, not published**. Latest published Python version: **0.10.0**.
Intended Python tag: `v0.11.0`. No `v0.11.0` tag, GitHub Release or PyPI artifact exists yet.

## Why 0.11.0 and not 0.10.1

The changes since `v0.10.0` add public API (`dprovenancekit.exporters`), new CLI options
(`dpk init --from-otel` / `--from-langsmith`, `dpk ingest --format langsmith`,
`dpk gate --otlp-out`) and new optional extras (`[otel]`, `[langsmith]`, `[langfuse]`,
`[phoenix]`, `[datadog]`). The [changelog](../CHANGELOG.md) follows Keep a Changelog and
aims to follow Semantic Versioning, where added functionality is a minor bump. A patch
number would advertise a fixes-only release. Existing APIs, Trace Specification v1,
DPK-BINARY-V1 and gate defaults are unchanged.

## Release scope

| Area | Change |
|---|---|
| Trace bootstrap | `dpk init --from-otel FILE...` / `--from-langsmith FILE...` pin a golden baseline from existing traces; `dpk ingest --format langsmith` |
| Verdict export | `dprovenancekit.exporters`: OTel span/event, stdlib OTLP JSON, LangSmith feedback, Langfuse scores, Phoenix annotations, Datadog evaluations |
| CLI | `dpk gate --otlp-out FILE`; `dpk gate --json` adds `golden_run_id`, `candidate_run_id`, `profile` |
| Quickstart fix | `dpk init`'s `DPK_QUICKSTART.md` failure walkthrough no longer causes `NameError` |
| Conformance | `otel_verdict_attributes.json` vector and the OTel GenAI mapping document |

The experimental event-chain prototype under `experimental/` is repository-only and is not
part of the `dprovenancekit` wheel.

## Cross-language note

[The release policy](RELEASE_POLICY.md) gives Python and Swift one shared product version.
Before announcing 0.11.0 as a product release, either prepare and publish Swift 0.11.0 under
the Swift repository's own checklist or describe this as a Python-only 0.11.0 release and
report Swift as remaining at 0.10.0. Matching numbers never imply feature parity: trace
bootstrap and the Python exporters are Python-only (see
[cross-language gaps](../conformance/CROSS_LANGUAGE_GAPS.md)).

## Python validation

Run against the exact commit intended for publication in an isolated environment:

```bash
python -m pip install -e ".[dev]" build twine ruff mypy
python scripts/check_release_version.py v0.11.0
python scripts/check_version_surface.py
python -m pytest -rs
ruff check dprovenancekit/
mypy dprovenancekit/
python -m build
python -m twine check dist/dprovenancekit-0.11.0*
```

Install the wheel into a fresh virtual environment outside the source checkout and verify:
metadata reports exactly 0.11.0, core dependencies remain empty,
`import dprovenancekit.exporters` succeeds without optional extras,
`dpk init --from-otel --help` works, and the `dpk init` quickstart walkthrough (record,
gate PASS, change the documented line to `is_active = True`, gate exits 1) behaves as
documented.

## Publication gates

- [ ] Merge this preparation PR after all required checks (including DCO) pass on its final SHA.
- [ ] Confirm `v0.11.0` does not already exist; never reuse or move a release tag.
- [ ] Publish Python `v0.11.0` with a GitHub Release targeting the merge commit on `main`;
      the Trusted Publishing workflow repeats the version check, tests, builds, attests and uploads.
- [ ] Install exactly `dprovenancekit==0.11.0` from PyPI in a fresh environment and repeat the wheel checks above.
- [ ] Only after PyPI verification: change the changelog heading to the publication date, update
      latest-published status in `README.md`, `docs/VERSION_SURFACE.md` and
      `docs/RELEASE_POLICY.md`, and drop the "install from source" notes.
- [ ] After PyPI verification, update downstream pins (the regression-gate Action) to 0.11.0.
- [ ] Decide on Swift 0.11.0 per the cross-language note before announcing a coordinated release.

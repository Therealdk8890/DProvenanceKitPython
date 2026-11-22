# Version surface

CI script `scripts/check_version_surface.py` fails when README / docs contradict
`pyproject.toml` + `dprovenancekit.__version__` fallback.

| Surface | Value | Source of truth |
|---------|-------|-----------------|
| Python package | `0.11.0` (prepared, unpublished) | `pyproject.toml` [project].version |
| `__version__` fallback | `0.11.0` | `dprovenancekit/__init__.py` |
| Latest published Python package | `0.10.0` | [PyPI](https://pypi.org/project/dprovenancekit/0.10.0/) |
| Swift package | `0.10.0` | [Swift release tag](https://github.com/Therealdk8890/DProvenanceKit/tree/0.10.0) |
| Latest published Swift package | `0.10.0` | [Swift GitHub Release](https://github.com/Therealdk8890/DProvenanceKit/releases/tag/0.10.0) |
| Trace Spec | v1 (frozen) | `conformance/TRACE_SPEC_v1.md` |
| Attestation encoding | `DPK-BINARY-V1` | `dprovenancekit.attestation` / Swift `docs/ATTESTATION.md` |

Attestation MVP ships in **0.7.0+** via `dprovenancekit[crypto]`. README capability matrix
must not claim Python attestation if `version < 0.7.0`.

The unpublished 0.9.3 preparation is superseded by the published coordinated 0.10.0
release. The multi-database workbench and opt-in reasoning repository are included in
Python 0.10.0; they are absent from the older 0.9.2 wheel. Matching product versions do
not imply feature parity or cross-language API compatibility; consult each SDK's
capability matrix and [the publication record](RELEASE_0.10.0.md).

Source is prepared as Python **0.11.0**: OTel/LangSmith trace bootstrap, verdict
exporters and `dpk gate --otlp-out` exist only in 0.11.0+. Latest-published rows change
only after PyPI publication is verified; see [the 0.11.0 checklist](RELEASE_0.11.0.md).

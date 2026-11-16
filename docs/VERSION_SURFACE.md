# Version surface

CI script `scripts/check_version_surface.py` fails when README / docs contradict
`pyproject.toml` + `dprovenancekit.__version__` fallback.

| Surface | Value | Source of truth |
|---------|-------|-----------------|
| Python package candidate | `0.10.0` (not yet on PyPI) | `pyproject.toml` [project].version |
| `__version__` fallback | `0.10.0` candidate | `dprovenancekit/__init__.py` |
| Latest published Python package | `0.9.2` | [PyPI](https://pypi.org/project/dprovenancekit/) |
| Swift package candidate | `0.10.0` (not yet tagged) | [Swift version surface](https://github.com/Therealdk8890/DProvenanceKit/blob/release/0.10.0-preparation/docs/VERSION_SURFACE.md) |
| Latest published Swift package | `0.9.2` | [Swift GitHub Release](https://github.com/Therealdk8890/DProvenanceKit/releases/tag/0.9.2) |
| Trace Spec | v1 (frozen) | `conformance/TRACE_SPEC_v1.md` |
| Attestation encoding | `DPK-BINARY-V1` | `dprovenancekit.attestation` / Swift `docs/ATTESTATION.md` |

Attestation MVP ships in **0.7.0+** via `dprovenancekit[crypto]`. README capability matrix
must not claim Python attestation if `version < 0.7.0`.

The unpublished 0.9.3 preparation is superseded by the coordinated 0.10.0 candidate.
Python 0.9.2 remains the latest published PyPI artifact and does not include the
multi-database workbench or reasoning repository merged after that release. Swift 0.9.2
is already published. Matching product versions do not imply feature parity or
cross-language API compatibility; consult each SDK's capability matrix and
[the release checklist](RELEASE_0.10.0.md).

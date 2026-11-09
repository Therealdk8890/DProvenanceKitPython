# Version surface

CI script `scripts/check_version_surface.py` fails when README / docs contradict
`pyproject.toml` + `dprovenancekit.__version__` fallback.

| Surface | Value | Source of truth |
|---------|-------|-----------------|
| Python package | `0.9.3` release candidate (not yet on PyPI) | `pyproject.toml` [project].version |
| `__version__` fallback | `0.9.3` release candidate | `dprovenancekit/__init__.py` |
| Swift package | `0.9.2` published; `0.9.3` is the next coordinated release target, not yet tagged | DProvenanceKit Swift release/tag |
| Trace Spec | v1 (frozen) | `conformance/TRACE_SPEC_v1.md` |
| Attestation encoding | `DPK-BINARY-V1` | `dprovenancekit.attestation` / Swift `docs/ATTESTATION.md` |

Attestation MVP ships in **0.7.0+** via `dprovenancekit[crypto]`. README capability matrix
must not claim Python attestation if `version < 0.7.0`.

The Python 0.9.3 entry describes an unreleased candidate. Python 0.9.2 remains the latest
published PyPI artifact and does not include the multi-database workbench merged after that
release. Swift 0.9.2 is already published. Matching product versions do not imply feature parity
or cross-language API compatibility; consult the capability matrix and each SDK's documentation.

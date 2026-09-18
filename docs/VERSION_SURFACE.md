# Version surface

CI script `scripts/check_version_surface.py` fails when README / docs contradict
`pyproject.toml` + `dprovenancekit.__version__` fallback.

| Surface | Value | Source of truth |
|---------|-------|-----------------|
| Python package | `0.7.1` | `pyproject.toml` `[project].version` |
| `__version__` fallback | same | `dprovenancekit/__init__.py` |
| Swift package | `0.8.1` | [DProvenanceKit](https://github.com/Therealdk8890/DProvenanceKit) `Version.swift` |
| Trace Spec | v1 (frozen) | `conformance/TRACE_SPEC_v1.md` |
| Attestation encoding | `DPK-BINARY-V1` | `dprovenancekit.attestation` / Swift `docs/ATTESTATION.md` |

Attestation MVP ships in **0.7.0+** via `dprovenancekit[crypto]`. README capability matrix
must not claim Python attestation if `version < 0.7.0`.

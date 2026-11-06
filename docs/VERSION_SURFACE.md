# Version surface

CI script `scripts/check_version_surface.py` fails when README / docs contradict
`pyproject.toml` + `dprovenancekit.__version__` fallback.

| Surface | Value | Source of truth |
|---------|-------|-----------------|
| Python package | `0.9.2` candidate (not yet on PyPI) | `pyproject.toml` `[project].version` |
| `__version__` fallback | `0.9.2` candidate | `dprovenancekit/__init__.py` |
| Swift package | `0.9.2` candidate (not yet tagged) | [DProvenanceKit PR #142](https://github.com/Therealdk8890/DProvenanceKit/pull/142), `Version.swift` |
| Trace Spec | v1 (frozen) | `conformance/TRACE_SPEC_v1.md` |
| Attestation encoding | `DPK-BINARY-V1` | `dprovenancekit.attestation` / Swift `docs/ATTESTATION.md` |

Attestation MVP ships in **0.7.0+** via `dprovenancekit[crypto]`. README capability matrix
must not claim Python attestation if `version < 0.7.0`.

The 0.9.2 entries describe coordinated release candidates, not published artifacts. Matching
product versions do not imply feature parity or cross-language API compatibility; consult the
capability matrix and each SDK's documentation.

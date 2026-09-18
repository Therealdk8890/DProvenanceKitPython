# Product layers — OSS evidence engine vs commercial AI Assurance Platform

This repository is the **Apache-2.0 evidence engine** (Python SDK). Commercial
platform capabilities live in **separate repositories under a proprietary
license**. They depend on these Apache SDKs; they do **not** relicense them.

> Positioning: DProvenanceKit is an **assurance / evidence layer under**
> Langfuse, OpenTelemetry, LangSmith, and similar tools — not a replacement
> dashboard.

## Free / commercial matrix

| Capability | Layer | License | Where |
|------------|-------|---------|--------|
| Record instrumented decision paths | OSS | Apache-2.0 | This repo / Swift twin |
| Local SQLite store (offline-first) | OSS | Apache-2.0 | This repo / Swift twin |
| Query / structural diff / align | OSS | Apache-2.0 | This repo / Swift twin |
| CI regression gate (`dpk gate`, Action) | OSS | Apache-2.0 | This repo + `dprovenancekit-action` |
| Software attestation (`DPK-BINARY-V1`) | OSS | Apache-2.0 | This repo (`[crypto]` extra) |
| Swift SE / CryptoKit attestation + proof packs | OSS | Apache-2.0 | `DProvenanceKit` (Swift) |
| OTel / OTLP export & ingest | OSS | Apache-2.0 | Both SDKs |
| Trace Spec + conformance vectors | OSS | Apache-2.0 | `conformance/` |
| Cloud federation / multi-tenant sync | Commercial | Proprietary | `dprovenancekit-server`, `DProvenanceKit-Premium` |
| Team workspace | Commercial | Proprietary | Platform repos |
| Evidence retention / lifecycle | Commercial | Proprietary | Platform repos |
| Policy management (org/project) | Commercial | Proprietary | Platform repos |
| Advanced semantic evaluation (hosted) | Commercial | Proprietary | Platform repos |
| Audit workflows (chained, multi-user) | Commercial | Proprietary | Platform repos |
| RBAC / enterprise SSO | Commercial | Proprietary | Platform repos |
| Enterprise deploy, SLA, support | Commercial | Proprietary | Services + Platform |
| Governed AI Deployment Pilot ($4,500) | Services | SOW | See Swift [`COMMERCIAL.md`](https://github.com/Therealdk8890/DProvenanceKit/blob/main/COMMERCIAL.md) |

## Apache-2.0 vs proprietary boundary

- **Everything in this public repository** is Apache-2.0 (`LICENSE`, `NOTICE`).
- **Premium / Platform / Cloud control-plane code** must not be merged here.
- Consuming a commercial product does **not** change the Apache license of the
  SDKs you already have. The commercial license covers only the proprietary
  components and services.

## Trust model (local-first)

1. **Local-first** — record and gate on-device / in your process.
2. **Optionally synced** — push selected evidence when *you* choose.
3. **Cryptographically verifiable** — attest when you need integrity proofs.

### What will never be required to leave the device

The OSS SDK never requires you to upload traces, payloads, or keys to a
DProvenance-operated service in order to:

- record runs
- pin a golden baseline
- diff / compare / gate in CI
- run anomaly rules
- export OTel JSON you control
- sign or verify software `DPK-BINARY-V1` attestations locally

Cloud sync (`dpk sync`) is an **optional** connector that only works when a
separate commercial client/package is installed. Default path: data stays local.

## Related commercial repositories

| Repo | Role |
|------|------|
| [`DProvenanceKit-Premium`](https://github.com/Therealdk8890/DProvenanceKit-Premium) | Proprietary stubs / premium connectors (private) |
| [`dprovenancekit-server`](https://github.com/Therealdk8890/dprovenancekit-server) | Proprietary cloud control plane (private) |

Pilot intake and commercial services: see the Swift repo’s
[COMMERCIAL.md](https://github.com/Therealdk8890/DProvenanceKit/blob/main/COMMERCIAL.md).

*Last updated: September 2026*

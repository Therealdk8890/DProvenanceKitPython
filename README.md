# 🚀 DProvenanceKit (Python)

[![CI](https://github.com/Therealdk8890/DProvenanceKitPython/actions/workflows/ci.yml/badge.svg)](https://github.com/Therealdk8890/DProvenanceKitPython/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/dprovenancekit)](https://pypi.org/project/dprovenancekit/)
[![License](https://img.shields.io/pypi/l/dprovenancekit)](LICENSE)
[![Listed in the official OpenAI Agents SDK docs](https://img.shields.io/badge/OpenAI%20Agents%20SDK-listed%20in%20the%20official%20docs-412991)](https://github.com/openai/openai-agents-python/blob/main/docs/tracing.md#external-tracing-processors-list)

## Start here

**[dpk-gate-demo](https://github.com/Therealdk8890/dpk-gate-demo)** — same answer, dropped `verify`, path gate fails.

Green CI: [gate #2](https://github.com/Therealdk8890/dpk-gate-demo/actions/runs/35685130719)

## Tamper-evident records of instrumented AI decision paths — offline-first storage, CI-gated

For teams in healthcare, finance, and legal building AI systems that need a local-first record of *which instrumented steps ran*, and a regression gate when that path drifts — without sending sensitive traces to a third-party SaaS by default.

> Working in Swift / on-device Apple AI? **[DProvenanceKit](https://github.com/Therealdk8890/DProvenanceKit)** — same recording / diff / gate model, plus CryptoKit attestation (`DPK-BINARY-V1` + ECDSA P-256 DER), proof packs, and a Foundation Models adapter. **Python can sign/verify the same `DPK-BINARY-V1` format** via `pip install dprovenancekit[crypto]` (software P-256; no Secure Enclave / proof packs).

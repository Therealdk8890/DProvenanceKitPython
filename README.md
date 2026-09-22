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

---

## The gap request-level observability leaves open

Your AI makes a decision that impacts a customer. The decision is challenged.

**Lender:** "Why did you reject this applicant?"
**Doctor:** "Why did you recommend that treatment?"
**Lawyer:** "What's the basis for this legal argument?"
**Auditor:** "Has this recorded decision path been altered since it was written?"

Request-level observability (OpenTelemetry, LangSmith, Langfuse, Datadog) remains essential for what happened in production. It does not, by itself, give you a **queryable, diffable, locally retained record of the instrumented decision path** — or a CI check that refuses merge when that path regresses. For regulated workflows, that gap matters: sensitive reasoning often cannot leave your infrastructure.

DProvenanceKit (Python) closes the recording / diff / gate gap locally. Software `DPK-BINARY-V1` attestation is available via the optional `[crypto]` extra; proof packs and Secure Enclave remain Swift-only.

**Product layers:** free Apache-2.0 evidence engine vs commercial AI Assurance Platform — see [docs/PRODUCT_LAYERS.md](docs/PRODUCT_LAYERS.md). Five-minute DX hook: `dpk init` then `python agent_stub.py` (or `python -m examples.e2e_decision_path`).

This package does **not** prove that model reasoning was “sound,” that every claim in a payload is true, or that a regulator will accept a trace as sufficient evidence. For the signed-artifact threat model (Swift), read [ATTESTATION — What it does not establish](https://github.com/Therealdk8890/DProvenanceKit/blob/main/docs/ATTESTATION.md#what-it-does-not-establish) (also summarized on [dprovenance.dev](https://dprovenance.dev)).

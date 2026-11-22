# DProvenanceKit (Python)

[![CI](https://github.com/Therealdk8890/DProvenanceKitPython/actions/workflows/ci.yml/badge.svg)](https://github.com/Therealdk8890/DProvenanceKitPython/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/dprovenancekit)](https://pypi.org/project/dprovenancekit/)
[![License](https://img.shields.io/pypi/l/dprovenancekit)](LICENSE)
[![Listed in the official OpenAI Agents SDK docs](https://img.shields.io/badge/OpenAI%20Agents%20SDK-listed%20in%20the%20official%20docs-412991)](https://github.com/openai/openai-agents-python/blob/main/docs/tracing.md#external-tracing-processors-list)

## Catch AI agent workflow regressions in CI

DProvenanceKit records the execution steps your application instruments, compares candidate runs with a known-good baseline, and can fail CI when a required step disappears or changes. It is a local-first, Apache-2.0 SDK that adds a regression gate alongside your existing tracing and observability tools.

## Try it in 60 seconds

The published **0.10.0 release** includes an **opt-in reasoning repository v1**:
immutable workflow/run commits, branches, baseline promotion,
read-only history browsing, and verified offline bundles. Run
`dpk repo demo --output-dir history-demo` after installing 0.10.0.
See the [local reasoning history guide](docs/REASONING_REPOSITORY.md).
This functionality requires 0.10.0 or later; it is absent from the older 0.9.2 wheel.

Install the published Python package and run the self-contained demo:

```bash
python -m pip install dprovenancekit
dpk demo
```

The demo uses sample traces—no model, API key, or network service is required. It deliberately removes a verification step, then demonstrates DPK detecting that regression. The demo exits successfully when it confirms the expected failure.

Want to see the gate on a pull request? Explore the [dpk-gate-demo repository](https://github.com/Therealdk8890/dpk-gate-demo) and its [CI run](https://github.com/Therealdk8890/dpk-gate-demo/actions/runs/35685130719).

## How DPK fits your existing stack

Keep the tools you already use:

- **LangSmith and Langfuse** help inspect and evaluate application traces.
- **OpenTelemetry** provides vendor-neutral telemetry collection and transport.
- **DProvenanceKit** records the instrumented workflow structure, compares it with a baseline, and applies a CI policy to decide whether a change should block release.

The distinction is the question each tool answers:

| | Observability and tracing | DProvenanceKit |
|---|---|---|
| Primary question | What happened during this run? | Did the instrumented workflow regress against its baseline? |
| Main use | Inspect, monitor, and investigate executions | Compare recorded paths and enforce regression policy in CI |
| Data flow | Your existing collectors and platforms | Local recording and comparison; optional OpenTelemetry ingestion |
| Relationship | Keep using your current stack | Add DPK alongside it |

Python integrations include LangChain/LangGraph, OpenAI Agents, LlamaIndex, CrewAI, and OpenTelemetry ingestion. Starting with **0.11.0** you can also start from traces you already have with `dpk init --from-otel` or `--from-langsmith`, and send gate verdicts back to OpenTelemetry, LangSmith, Langfuse, Arize Phoenix or Datadog with [`dprovenancekit.exporters`](docs/OBSERVABILITY_EXPORTERS.md). These are not in the published 0.10.0 package; until 0.11.0 is on PyPI, install from `main` to try them (`pip install "git+https://github.com/Therealdk8890/DProvenanceKitPython.git"`). DPK complements these systems; it does not replace production monitoring or evaluation.

## Example: protect a legal-document workflow

Imagine a team uses an AI assistant to prepare a first draft of a legal brief. The application instruments three checks before export:

1. Confirm that each cited case can be found in the selected legal source.
2. Check that cited statutes are current for the relevant jurisdiction.
3. Compare quoted passages with the source text.

The team records a known-good run and makes the required checks part of its regression policy. Later, a prompt, model, or application change accidentally removes the statute-currentness check. DPK can identify the missing instrumented step and fail the CI gate before that change ships.

The resulting record helps the team review which checks were captured and how the candidate path differed from its baseline. It does not independently validate the legal sources or establish that the brief is legally correct.

## Python and Swift capabilities

The SDKs share a product and trace model, but their language-specific capabilities differ. Python has a broader set of framework integrations; Swift provides Apple-platform signing and proof-pack capabilities.

| Capability | Python SDK | Swift SDK |
|---|---|---|
| Record, query, and diff instrumented paths | Yes | Yes |
| Golden-baseline regression gate | Yes | Yes |
| Framework and telemetry integrations | LangChain/LangGraph, OpenAI Agents, LlamaIndex, CrewAI, OpenTelemetry ingest | Foundation Models adapter; OpenTelemetry bridge |
| `DPK-BINARY-V1` trace attestation | Yes; optional `[crypto]` extra, software P-256 keys | Yes; CryptoKit and P-256 |
| Proof packs | Not currently available | Yes |
| Secure Enclave-backed keys | Not available | Supported Apple hardware |
| Portable reasoning-history objects and bundle verification | Yes | Yes; read-only verification |
| Local reasoning-history operations and web workbench | Yes; opt-in | Not available |
| Structural Alignment v1 and signed configuration manifests | Experimental, opt-in | Experimental, opt-in |

Python's optional cryptographic extra is installed separately:

```bash
python -m pip install "dprovenancekit[crypto]"
```

For Swift and Apple on-device AI, see the [DProvenanceKit Swift SDK](https://github.com/Therealdk8890/DProvenanceKit). For the exact cross-language contract, see [TRACE_SPEC_v1](conformance/TRACE_SPEC_v1.md) and the [release policy](https://github.com/Therealdk8890/DProvenanceKit/blob/main/docs/RELEASE_POLICY.md).

## Scope and limits

DPK evaluates the execution data your application chooses to instrument and the regression policies you configure.

- A recorded or matching path does not prove that model reasoning was sound or that a claim, citation, or decision is true.
- A missing event means it was not present in the recorded instrumented path; it does not prove that an uninstrumented action never occurred.
- Cryptographic attestation verifies the integrity of the artifact against its signature. It does not, by itself, establish signer identity, trusted time, or regulatory acceptance.
- DPK is not a certification, compliance determination, or substitute for domain review.

These boundaries are intentional. See the [attestation threat model and limitations](https://github.com/Therealdk8890/DProvenanceKit/blob/main/docs/ATTESTATION.md#what-it-does-not-establish).

## Documentation

- [Python installation and API documentation](https://dprovenance.dev)
- [Regression-gate GitHub Action](https://github.com/marketplace/actions/dprovenancekit-regression-gate)
- [Observability exporters and trace bootstrap](docs/OBSERVABILITY_EXPORTERS.md) — gate verdicts in OTel, LangSmith, Langfuse, Phoenix and Datadog (0.11.0+)
- [Self-hosted trace workbench guide](docs/SELF_HOSTED_WORKBENCH.md) — installation for the published multi-database viewer
- [0.10.0 release checklist](docs/RELEASE_0.10.0.md) — scope, validation and publication gates
- [0.11.0 release checklist](docs/RELEASE_0.11.0.md) — prepared candidate; not yet published
- [Product and release documentation](docs/)
- [DProvenanceKit alongside LangSmith](https://dprovenance.dev/compare/dprovenancekit-vs-langsmith/)

## Release status and license

The latest published Python and Swift SDK releases are **0.10.0**:
[Python GitHub Release](https://github.com/Therealdk8890/DProvenanceKitPython/releases/tag/v0.10.0),
[PyPI](https://pypi.org/project/dprovenancekit/0.10.0/) and
[Swift GitHub Release](https://github.com/Therealdk8890/DProvenanceKit/releases/tag/0.10.0).
This release includes the multi-database workbench and opt-in reasoning history;
Structural Alignment v1/configuration authorization remain experimental. The earlier
unpublished 0.9.3 preparation is superseded. See the
[publication record](docs/RELEASE_0.10.0.md) and [workbench guide](docs/SELF_HOSTED_WORKBENCH.md).

Source on `main` is prepared as **0.11.0** (trace bootstrap with `dpk init --from-otel` /
`--from-langsmith`, `dprovenancekit.exporters`, `dpk gate --otlp-out`). 0.11.0 is not
published yet; see the [0.11.0 release checklist](docs/RELEASE_0.11.0.md).

DProvenanceKit is released under the [Apache License 2.0](LICENSE).

## Contributing and support

- [Report an issue or request an integration](https://github.com/Therealdk8890/DProvenanceKitPython/issues)
- [Python SDK repository](https://github.com/Therealdk8890/DProvenanceKitPython)
- [Swift SDK repository](https://github.com/Therealdk8890/DProvenanceKit)
- [Project documentation](https://dprovenance.dev)

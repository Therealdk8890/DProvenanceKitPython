# 🚀 DProvenanceKit (Python)

[![CI](https://github.com/Therealdk8890/DProvenanceKitPython/actions/workflows/ci.yml/badge.svg)](https://github.com/Therealdk8890/DProvenanceKitPython/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/dprovenancekit)](https://pypi.org/project/dprovenancekit/)
[![License](https://img.shields.io/pypi/l/dprovenancekit)](LICENSE)
[![Listed in the official OpenAI Agents SDK docs](https://img.shields.io/badge/OpenAI%20Agents%20SDK-listed%20in%20the%20official%20docs-412991)](https://github.com/openai/openai-agents-python/blob/main/docs/tracing.md#external-tracing-processors-list)

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

---

## Built for teams in regulated industries

### Healthcare
Diagnostic-support and clinical decision tools need a durable record of which checks ran and what evidence was attached — retained locally and comparable across releases.

### Financial Services
Lending and underwriting workflows need an auditable trail of the instrumented factors that entered a decision, plus a CI gate when that trail changes after a model or prompt update.

### Legal
Brief-generation and citation workflows need a retained chain of verification steps your team instrumented — not a promise that citations are correct, but a record you can review and diff.

### Insurance
Claims workflows need a consistent, queryable decision path for appeals and internal audit — and a regression signal when automation drifts.

### Government
Eligibility and records workflows often cannot ship raw reasoning to a hosted SaaS. Local-first recording keeps data where policy requires.

---

## How It Works

**DProvenanceKit is not a SaaS platform. It's a local-first SDK.**

1. **Your AI system runs normally.** Everything stays on your infrastructure.

2. **Each instrumented decision path is recorded locally.**
   - Reasoning steps you wrap or emit
   - Evidence and tool calls you record
   - Intermediate results you choose to capture

3. **Compare and gate.**
   - Diff a candidate run against a golden baseline
   - Fail CI when the path regresses beyond policy (`dprovenancekit gate`, pytest `golden_trace`, or the [GitHub Action](https://github.com/marketplace/actions/dprovenancekit-regression-gate))
   - Action default pin: `dprovenancekit==0.7.1` (prefer also pinning the Action to a commit SHA — [dprovenancekit-action](https://github.com/Therealdk8890/dprovenancekit-action))

4. **Attest offline when you need a signature.**
   - Python: `pip install dprovenancekit[crypto]` then `dpk attest sign|verify` (software P-256, same `DPK-BINARY-V1` bytes as Swift)
   - Swift: CryptoKit + optional Secure Enclave + proof packs
   - Same Trace Spec for fingerprints / query / alignment across languages

---

## Swift vs Python capability matrix

| Capability | Swift (`DProvenanceKit`) | Python (`dprovenancekit`) |
|---|---|---|
| Record / store / query instrumented paths | Yes | Yes |
| Semantic diff + golden baselines | Yes | Yes |
| CI regression gate | Yes | Yes |
| Framework adapters | Foundation Models (+ OTel bridge) | LangChain, OpenAI Agents, LlamaIndex, CrewAI, OTel ingest |
| Trace attestation (`DPK-BINARY-V1` + P-256 DER) | **Yes** | **MVP yes** (`[crypto]` extra; software keys; **0.7.0+**) |
| Proof packs | **Yes** | **Not yet** |
| Secure Enclave–backed keys | **Yes** (Apple platforms) | N/A |

Cross-language conformance covers fingerprints, query semantics, profile hash, and alignment verdicts per [TRACE_SPEC_v1](conformance/TRACE_SPEC_v1.md). Payload encodings need **not** be byte-identical across SDKs; equivalence is on decoded payloads and structural fingerprints.

---

## Works with your observability stack

DProvenanceKit aims to be the local-first layer for **AI decision-path observability** — record, diff, and CI-gate the instrumented path (attest on Swift). It is built to sit **beside** OpenTelemetry, LangSmith, Langfuse, and Arize, not to replace them.

| | Platform / request observability (LangSmith, Langfuse, OTel, …) | DProvenanceKit |
|---|---|---|
| **Job** | Spans, dashboards, evals, production monitoring | Decision path, golden baselines, CI gate (signed attestation on Swift) |
| **Question** | What happened? | Did the instrumented decision path regress? |
| **Where data lives** | Collector or hosted platform (by design) | Local-first; optional OTel ingest/export when you choose |
| **How they fit** | Keep using them | Add DPK next to them |

**Bottom line:** Use LangSmith, Langfuse, and OpenTelemetry for platform observability. Use DProvenanceKit when you need the decision path to be queryable, diffable, and refused in CI when it drifts. Use `dprovenancekit[crypto]` for software attestation; use the Swift SDK when you also need proof packs or Secure Enclave keys.

More detail: [DProvenanceKit alongside LangSmith](https://dprovenance.dev/compare/dprovenancekit-vs-langsmith/).

---

## Real Example: Legal Document Provenance

A law firm uses an AI to draft legal briefs.

**The problem:** Every citation must be verifiable. If the AI cites a case that doesn't exist, that's malpractice.

**How DProvenanceKit helps:**

```
1. Legal AI generates brief
2. Before export, instrumented verification steps are recorded:
   - Case existence check against a legal database
   - Statute currency check
   - Quote accuracy check against source
3. Python: baseline + CI gate catch path regressions across releases
4. Swift (optional): attest the recorded chain and attach a proof pack
5. If disputed, the firm can show:
   - The instrumented reasoning chain that was recorded
   - Diffs against the golden path
   - (Swift) Offline verification that the attested record was not altered after signing
```

Recording and gating do not prove citations are correct. Attestation (Swift) establishes integrity of what was recorded — see [What it does not establish](https://github.com/Therealdk8890/DProvenanceKit/blob/main/docs/ATTESTATION.md#what-it-does-not-establish).

---

## Open Source + Paid Governance Support

### Option 1: Self-Directed (Open Source)

DProvenanceKit is Apache 2.0 licensed. You can use it free:

```bash
# Python
pip install dprovenancekit

# Swift
dependencies: [
    .package(url: "https://github.com/Therealdk8890/DProvenanceKit", from: "0.8.1")
]
```

You instrument your AI workflow. You establish baselines. You manage the governance policy.

**Best for:** Teams with internal compliance/audit expertise.

### Option 2: Governed AI Deployment Pilot ($4,500 one-time)

For organizations that want governance guidance and a structured review of one AI workflow:

**Includes:**
- **Instrumentation review:** Is this the right tracing for your compliance needs?
- **Baseline establishment:** What's the "golden" reasoning path your AI should follow?
- **Governance policy definition:** What counts as a regression? When do we alert? What's audit-worthy?
- **Compliance-oriented audit report:** A written summary of your reasoning architecture and how DPK artifacts support review — not certification, indemnity, or a guarantee of regulatory acceptance.

**Does not include:**
- Recurring SaaS or managed service
- Code in your repository
- Ongoing support (scope separately as needed)
- Certification under any legal or industry framework

**Who this is for:** Chief Risk Officer, Compliance Officer, Audit Manager at an organization in a regulated industry deploying one specific AI workflow.

**Example scope:**
- Healthcare: Diagnostic-recommendation AI
- Finance: Lending decision AI
- Legal: Brief-generation AI
- Insurance: Claims-approval AI

**Timeline:** 30 days, delivered as a report.

**Next step:** [Request a pilot](mailto:inquiry@dprovenance.dev?subject=Governed%20AI%20Deployment%20Pilot).

---

## Getting Started

### For Open-Source Users

1. **Define your AI's critical decisions**
   - Which instrumented steps must appear in an audit trail?
   - What evidence matters?
   - Where is liability highest?

2. **Instrument one workflow**

```bash
pip install dprovenancekit
# optional adapters:
# pip install "dprovenancekit[langchain]" "dprovenancekit[openai-agents]"
```

```python
from dprovenancekit import traced, record_event, traced_run

@traced
def check_credit(applicant):
    ...

@traced
def verify_income(applicant):
    ...

with traced_run(context_id="applicant_12345"):
    check_credit(data)
    verify_income(data)
    record_event("decision_made", {"approved": True})
```

See the catch immediately after installing:

```bash
dprovenancekit demo
```

Full record → baseline → gate (HIGH fail) → attest → verify walkthrough:
[`examples/e2e_decision_path/`](examples/e2e_decision_path/) (`pip install -e ".[crypto]"` then `python -m examples.e2e_decision_path`).

3. **Establish a baseline**
   - Run your workflow multiple times
   - Pin a known-good run (`dpk record`)
   - Store the baseline with the repo

4. **Gate future changes**
   - When you update the model, re-run
   - Compare new reasoning to baseline (`dpk compare` / `dpk gate`)
   - Diff shows exactly what changed
   - Decide: Is this safe to deploy?

5. **When you need cryptographic attestation**
   - Python: `pip install dprovenancekit[crypto]` and `dpk attest sign|verify`
   - Swift: full CryptoKit path + proof packs / Secure Enclave in the [Swift SDK](https://github.com/Therealdk8890/DProvenanceKit)
   - Limits: [ATTESTATION.md](https://github.com/Therealdk8890/DProvenanceKit/blob/main/docs/ATTESTATION.md#what-it-does-not-establish)

Adapters for LangChain / LangGraph, OpenAI Agents SDK, LlamaIndex, CrewAI, and OpenTelemetry ingest live in `dprovenancekit.integrations`. Details: [docs](https://dprovenance.dev) and the [OpenAI Agents listing](https://github.com/openai/openai-agents-python/blob/main/docs/tracing.md#external-tracing-processors-list).

### For Pilot Participants

1. Schedule a kickoff call
2. Define the scope (one AI workflow)
3. Provide your reasoning trace format
4. Receive governance policy + audit report
5. Keep the open-source tool, informed by compliance-oriented review

---

## Technical Foundation

- **Recording:** Non-blocking writes with priority-aware backpressure
- **Storage:** WAL-mode SQLite (crash-safe, auditable)
- **Query language:** Temporal and structural reasoning patterns
- **Diffing:** Semantic alignment engine that detects regressions
- **CI gate:** `dprovenancekit gate`, pytest `golden_trace`, and the [GitHub Action](https://github.com/marketplace/actions/dprovenancekit-regression-gate)
- **Attestation:** Both SDKs — `DPK-BINARY-V1` + ECDSA P-256 (DER); see [Swift ATTESTATION.md](https://github.com/Therealdk8890/DProvenanceKit/blob/main/docs/ATTESTATION.md) and `dprovenancekit.attestation`. Python MVP is software keys only (`[crypto]`). **Proof packs / Secure Enclave:** Swift-only. Neither claims JCS/RFC 8785 or Detached JWS.

**Cross-language:** Swift and Python stay aligned via a formal Trace Spec and shared conformance vectors (fingerprint, query, profile hash, alignment). Payload bytes need not match across languages; see [TRACE_SPEC §2](conformance/TRACE_SPEC_v1.md).

**Quality bar:** Conformance suite and benchmark corpus exercise edge cases. Built for teams in regulated industries; not a claim of production certification or regulator endorsement.

---

## No Third-Party Dependencies

**Core (Python):**
```
sqlite3, contextvars, threading, json, hashlib, uuid, urllib
```

No pip dependencies in the core. Just Python's standard library. Requires Python 3.9+.

**Core (Swift):**
```
Foundation, CryptoKit, SQLite
```

No external packages. Native to macOS/iOS. Requires Swift 6.0.

**Why this matters for compliance:** Fewer dependencies = smaller attack surface = easier for auditors to review.

---

## Adoption Path

### Week 1
Integrate DProvenanceKit into one AI workflow. Record a baseline.

### Week 2-4
Establish governance policy. Define what counts as a regression.

### Month 1-3
Gate releases on instrumented-path changes. Use `dprovenancekit[crypto]` for software attestation; add Swift when you need proof packs or Secure Enclave.

### Ongoing
Every release: baseline vs. candidate. A clear record of whether the instrumented path stayed consistent.

---

## Status

**Public beta — [0.7.0](https://github.com/Therealdk8890/DProvenanceKitPython/releases/tag/v0.7.0) on PyPI (`dprovenancekit`) adds MVP `DPK-BINARY-V1` software attestation via `dprovenancekit[crypto]`. APIs may continue to evolve before 1.0. Wheels older than 0.7.0 do not include attestation.**

---

## License

Apache 2.0. Free for commercial use.

---

## Contact

**For pilot inquiry:**
[Request Governed AI Deployment Pilot](mailto:inquiry@dprovenance.dev?subject=Governed%20AI%20Deployment%20Pilot)

**For open-source questions:**
GitHub Issues: https://github.com/Therealdk8890/DProvenanceKitPython

**For technical details:**
- Python: https://github.com/Therealdk8890/DProvenanceKitPython
- Swift: https://github.com/Therealdk8890/DProvenanceKit
- Docs: https://dprovenance.dev
- Attestation limits (Swift): https://github.com/Therealdk8890/DProvenanceKit/blob/main/docs/ATTESTATION.md

---

## Why This Exists

AI systems make decisions that affect real people. Teams in regulated industries need a durable, local record of instrumented decision paths — and a way to catch regressions before release. Cloud-based observability platforms aren't designed for that job alone.

DProvenanceKit is built for teams that care about:
- **Privacy:** Data stays local unless you explicitly export
- **Auditability:** Paths are queryable and diffable (cryptographically attestable on Swift)
- **Change control:** CI can refuse merges when the golden path drifts
- **Honest scope:** Artifacts support audit workflows; they are not by themselves certification or proof that a decision was “sound”

If your AI makes healthcare, financial, legal, or insurance decisions, start with one workflow and a golden baseline.
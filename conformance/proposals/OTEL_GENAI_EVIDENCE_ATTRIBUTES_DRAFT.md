# DRAFT: GenAI evidence and path-check attributes for OpenTelemetry

> **Status: internal draft, not submitted.** This is a starting point for a possible
> issue or pull request against
> [`open-telemetry/semantic-conventions-genai`](https://github.com/open-telemetry/semantic-conventions-genai).
> Nothing here has been discussed with the GenAI SIG. Before filing, check the current
> state of the evaluation, agent-span and stabilization work (the agentic-conventions
> stabilization tracking issue is a good place to start) and rewrite against whatever
> has landed by then.
>
> DProvenanceKit already emits the vendor-namespaced `dpk.*` equivalents defined in
> [`../OTEL_GENAI_MAPPING_v1.md`](../OTEL_GENAI_MAPPING_v1.md) §3, so nothing in the
> SDKs depends on this proposal being accepted.

## Problem

Agent observability today records *what happened* (spans for inference, tool calls,
agent invocations) and, through the `gen_ai.evaluation.result` event, *how good an
output was*. Two things that teams increasingly need cannot be expressed in a
vendor-neutral way:

1. **Path checks.** An evaluation that judges whether the agent took the required
   steps, not whether its final answer looks right. An agent can skip a verification
   tool call and still produce a plausible answer; output-scoring evaluators pass it.
   Regression gates in CI compare a run's step structure with a known-good baseline,
   and their result is about a whole trace, not one response.
2. **Verifiable evidence.** A reference to tamper-evident evidence about the run (a
   signed digest of the recorded steps, the signer's key id, the attestation format),
   so a reviewer or auditor can check offline that the trace being shown matches what
   was recorded. Regulation is pushing this direction: for example, Article 12 of the
   EU AI Act requires automatic event logging for high-risk systems.

Today each tool invents private attributes for both (DProvenanceKit uses `dpk.*`),
so a backend cannot show "this run failed its path check" or "this trace has a valid
signature" across tools.

## Proposal sketch

### A. Reuse `gen_ai.evaluation.result` for path checks; add three optional attributes

Keep the existing event and its `name` / `score.value` / `score.label` /
`explanation` attributes. Add:

| Attribute | Type | Requirement | Description |
| --- | --- | --- | --- |
| `gen_ai.evaluation.scope` | string enum | Recommended | What was evaluated: `response` (default today), `span`, `trace`. A path check is `trace`. |
| `gen_ai.evaluation.baseline.trace_id` | string | Conditionally required when the evaluation compares against a reference run | Trace id of the reference (golden) run. |
| `gen_ai.evaluation.reference.missing_steps` | string[] | Opt-in | Identifiers of required steps absent from the evaluated trace. |

Rationale: Datadog's evaluation API already distinguishes span and trace scope
(`eval_scope`), and LangSmith, Langfuse and Phoenix all attach evaluations at trace
level. A shared `scope` attribute lets backends render trace-level checks correctly.

### B. A small `gen_ai.evidence.*` namespace

| Attribute | Type | Requirement | Description |
| --- | --- | --- | --- |
| `gen_ai.evidence.format` | string | Required if any `gen_ai.evidence.*` is set | Evidence format identifier, e.g. `DPK-BINARY-V1`, or an in-toto/DSSE media type. |
| `gen_ai.evidence.digest` | string | Recommended | Digest of the evidence payload as `<algorithm>:<hex>`, e.g. `sha256:9f86…`. |
| `gen_ai.evidence.signer.key_id` | string | Recommended | Stable identifier of the signing key (e.g. SHA-256 of the public key). |
| `gen_ai.evidence.uri` | string | Opt-in | Where the full evidence bundle can be fetched. |
| `gen_ai.evidence.verified` | boolean | Opt-in | Set only by a verifier that checked the signature, never by the producer. |

Placement: on the root span of the run the evidence describes, or on a
`gen_ai.evaluation.result` event with scope `trace`.

Explicit non-goals: no signature bytes or key material in attributes; no claim that a
signature makes the agent's output *correct*. Evidence proves integrity and
provenance of the record, not the truth of its content.

## Mapping from today's `dpk.*` attributes

| `dpk.*` (v1, shipping) | Proposed |
| --- | --- |
| `dpk.gate.verdict`, `dpk.gate.passed` | `gen_ai.evaluation.score.label`, `score.value` (already emitted) |
| *(implicit: whole run)* | `gen_ai.evaluation.scope = trace` |
| `dpk.golden.run_id` (UUID of the baseline trace id) | `gen_ai.evaluation.baseline.trace_id` (32-hex) |
| `dpk.gate.steps.removed` | `gen_ai.evaluation.reference.missing_steps` |
| `dpk.evidence.attestation.format` | `gen_ai.evidence.format` |
| `dpk.evidence.attestation.sha256` | `gen_ai.evidence.digest` (`sha256:` prefix) |
| `dpk.evidence.signer.key_id` | `gen_ai.evidence.signer.key_id` |

If accepted, DProvenanceKit would emit both spellings during a deprecation window and
add the upstream names to `vectors/otel_verdict_attributes.json` under a new schema
version.

## Open questions for the SIG

1. Should path checks be a new event (`gen_ai.path_check.result`) rather than a scoped
   evaluation? Reusing the evaluation event keeps backends simple; a new event is
   clearer about semantics.
2. Is `gen_ai.evidence.*` GenAI-specific, or should it be a general `evidence.*` /
   supply-chain namespace shared with build provenance (SLSA, in-toto)?
3. How should evidence be attached when the run spans several traces (long-running
   agents, handoffs)? Span links are the obvious candidate.

## Checklist before submitting

- [ ] Re-read the current `gen-ai-events.md` and `gen-ai-agent-spans.md` on upstream `main`.
- [ ] Search existing issues for evaluation scope, trace-level evaluation, and evidence or attestation.
- [ ] Attend or watch a GenAI SIG call; ask whether an issue or a PR is preferred.
- [ ] Get Daniel's sign-off on the wording and on representing DProvenanceKit upstream.

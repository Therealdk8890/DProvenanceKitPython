# DProvenanceKit ↔ OpenTelemetry GenAI mapping, v1

Companion to [`TRACE_SPEC_v1.md`](TRACE_SPEC_v1.md). It defines how a DProvenanceKit (DPK)
trace relates to the OpenTelemetry GenAI semantic conventions in both directions:

- **Ingest** (§2): OTel GenAI spans → DPK runs and events. Implemented in Python by
  `dprovenancekit.otel_ingest` / `dpk ingest`.
- **Export** (§3): DPK gate verdicts → OTel span attributes and events. Implemented in
  Python by `dprovenancekit.exporters` / `dpk gate --otlp-out`.

> **Status:** v1. §3 is pinned by
> [`vectors/otel_verdict_attributes.json`](vectors/otel_verdict_attributes.json) and
> checked by `tests/test_conformance.py`. §2 is described here and enforced by
> `tests/test_otel_ingest.py`; it is not part of the cross-language vector set yet.
>
> **Upstream status:** the OTel GenAI conventions moved to
> [`open-telemetry/semantic-conventions-genai`](https://github.com/open-telemetry/semantic-conventions-genai)
> and are all at **Development** stability, with no stable release. Everything in this
> document that cites a `gen_ai.*` name tracks that repository's `main` branch as of
> November 2026 and may need revision when it stabilizes. The `dpk.*` names are
> DProvenanceKit's own and are stable under this v1 contract.

---

## 1. Identity: one run is one trace

| DPK concept (TRACE_SPEC) | OpenTelemetry | Rule |
| --- | --- | --- |
| Run (`run_id`, UUID) | Trace (`trace_id`, 16 bytes) | An ingested trace's run id **is** its trace id rendered as a UUID: a 32-hex id maps to `UUID(hex=…)`; a base64 16-byte id decodes to the same UUID; anything else maps to `uuid5(6f1a0f2e-2b8c-5e3a-9d47-0e10a1b2c3d4, id)`. |
| Event `span_id` / `parent_span_id` | Span `span_id` / `parent_span_id` | Copied verbatim from the source span. |
| Event `sequence` | *(none)* | OTel spans carry no causal sequence, so DPK reconstructs it: depth-first walk of the span tree, siblings ordered by start time, emitting `<kind>.start`, the children, then `<kind>.end` (or `<kind>.error`). |
| Event `engine` | Component identity attribute | Per step kind, the first present of the keys in §2.2; otherwise the span name with a known operation prefix stripped. |
| Context id | Root span name → `service.name` → trace id | Overridable with `dpk ingest --context`. |

Because the run id is the trace id, a verdict about an ingested run can always be joined
back to the trace it came from. §3.4 relies on this.

## 2. Ingest: GenAI spans → DPK steps

### 2.1 Step kind

The first rule that matches decides the step kind. Spans matching none are dropped
(or kept as `span.*` TELEMETRY events with `--include-unclassified`), so they never
affect fingerprints.

| Precedence | Source attribute | Values → DPK step kind |
| --- | --- | --- |
| 1 | `openinference.span.kind` (OpenInference / Arize Phoenix) | `LLM`→`llm_call`, `TOOL`→`tool_call`, `AGENT`→`agent_invocation`, `CHAIN`/`EVALUATOR`/`PROMPT`→`chain`, `RETRIEVER`→`retrieval`, `EMBEDDING`→`embedding`, `RERANKER`→`rerank`, `GUARDRAIL`→`guardrail`; unknown → `chain` |
| 2 | `traceloop.span.kind` (OpenLLMetry) | `tool`, `agent`, `workflow`/`task`→`chain` |
| 3 | `gen_ai.operation.name` (OTel GenAI) | `chat`/`text_completion`/`generate_content`→`llm_call`, `embeddings`→`embedding`, `retrieval`→`retrieval`, `execute_tool`→`tool_call`, `invoke_agent`→`agent_invocation`, `invoke_workflow`→`chain`, `create_agent`→`agent_creation`; other values → `llm_call` if `gen_ai.request.model` is set, else `chain` |
| 4 | `llm.request.type` (legacy OpenLLMetry) | `chat`/`completion`→`llm_call`, `embedding`, `rerank` |
| 5 | `ai.operationId` / `operation.name` (Vercel AI SDK) | `ai.toolCall*`, `ai.embed*`, `ai.generate*`/`ai.stream*` |
| 6 | Presence sniffing | `gen_ai.tool.name`→`tool_call`; `gen_ai.agent.name`→`agent_invocation`; `gen_ai.request.model`, `gen_ai.response.model`, `gen_ai.usage.*`, `gen_ai.system`, `gen_ai.prompt.*`→`llm_call` |
| 7 | Span name `"{operation} {target}"` | Same operation table as rule 3 |

Every classified span is STRUCTURAL priority; an error status yields a CRITICAL
`<kind>.error` event.

### 2.2 Engine (component identity)

| Step kind | Keys, in precedence order |
| --- | --- |
| `llm_call` | `gen_ai.request.model`, `llm.model_name`, `ai.model.id`, `gen_ai.response.model`, `gen_ai.provider.name`, `gen_ai.system` |
| `tool_call` | `gen_ai.tool.name`, `tool.name`, `traceloop.entity.name`, `ai.toolCall.name` |
| `agent_invocation` | `gen_ai.agent.name`, `agent.name`, `traceloop.entity.name` |
| `chain` | `gen_ai.workflow.name`, `traceloop.entity.name`, `graph.node.name` |
| `retrieval` | `gen_ai.data_source.id`, `embedding.model_name` |
| `embedding`, `rerank`, `guardrail`, `agent_creation` | see `_ENGINE_KEYS` in `dprovenancekit/otel_ingest.py` |

The fingerprint signature is `type::engine` (TRACE_SPEC §5). An agent traced through
OpenInference and the same agent traced through the official `gen_ai.*` conventions
therefore normalize to comparable step streams.

### 2.3 Known ingest gaps against upstream `main`

These upstream additions are not classified specially yet. They fall through to the
generic rules above, which is deterministic but less precise:

- `gen_ai.operation.name = plan` (agent planning span): becomes `llm_call` when a model
  is set, else `chain`.
- Memory operations (`gen_ai.memory.*`): `chain`.
- `gen_ai.agent.id` / `gen_ai.agent.version` / `gen_ai.conversation.id` are not part of
  the step identity. Adding them would change fingerprints, so it requires a
  reviewed vector change (TRACE_SPEC v2 or an explicit opt-in).

## 3. Export: gate verdict → OTel attributes and events

A gate verdict is exported in up to three forms, all carrying the same facts:

1. **Span attributes** (`dpk.*`, §3.2) on an existing span or a dedicated `dpk.gate` span.
2. A **`dpk.gate.verdict` span event** with the same attributes.
3. The standard GenAI **`gen_ai.evaluation.result` event** (§3.3), so convention-aware
   backends display the verdict without knowing anything about `dpk.*`.

### 3.1 Value rules

- Every value MUST be an OTel primitive (`string`, `boolean`, `int64`, `double`) or a
  homogeneous `string[]`. `null` is never emitted; an unknown optional field is omitted.
- Free text (`dpk.gate.reasoning`, the evaluation explanation) is truncated to **1024
  Unicode code points**, the last of which is `…` (U+2026) when truncation happened.
- Step lists are truncated to their first **64** items. `dpk.gate.steps.divergent_count`
  always carries the untruncated count.
- Key order is not significant.

### 3.2 `dpk.*` attributes (schema `dpk.gate.verdict.v1`)

| Key | Type | Requirement | Meaning |
| --- | --- | --- | --- |
| `dpk.schema` | string | Required | Always `dpk.gate.verdict.v1`. |
| `dpk.sdk.name` | string | Required | Emitting SDK, e.g. `dprovenancekit-python`. Not pinned by vectors. |
| `dpk.sdk.version` | string | Required | Emitting SDK version. Not pinned by vectors. |
| `dpk.gate.passed` | boolean | Required | The verdict. |
| `dpk.gate.verdict` | string | Required | `pass` or `fail`. |
| `dpk.gate.regression_level` | string | If known | `none` \| `low` \| `medium` \| `high`. |
| `dpk.gate.strength` | double | If known | Regression-risk strength, `[0, 1]`. |
| `dpk.gate.fingerprint_match` | boolean | If known | Golden and candidate fingerprints equal (TRACE_SPEC §5). |
| `dpk.gate.policy.max_regression_level` | string | If known | Worst severity that still passes. |
| `dpk.gate.policy.allow_divergent_steps` | boolean | If known | Whether per-step changes were tolerated. |
| `dpk.gate.policy.profile` | string | If known | Alignment profile, e.g. `strict_audit_v1`. |
| `dpk.golden.run_id` / `dpk.candidate.run_id` | string | If known | Run ids (UUID text). |
| `dpk.golden.fingerprint` / `dpk.candidate.fingerprint` | string | If known | Run fingerprints (40-hex SHA-1). |
| `dpk.gate.steps.removed` | string[] | If non-empty | Step `type_identifier`s present in golden, missing in candidate. |
| `dpk.gate.steps.added` | string[] | If non-empty | Steps new in the candidate. |
| `dpk.gate.steps.divergent` | string[] | If non-empty | All failing step changes: removed, added, reordered, ambiguous, in that order. |
| `dpk.gate.steps.divergent_count` | int | If divergent is non-empty | Untruncated length of the divergent list. |
| `dpk.gate.reasoning` | string | If non-empty | Engine explanation (truncated, §3.1). |
| `dpk.evidence.<key>` | string | Optional | Evidence references supplied by the caller. Keys use `[A-Za-z0-9_.]`. Recommended: `attestation.format` (e.g. `DPK-BINARY-V1`), `attestation.sha256`, `signer.key_id`. |

### 3.3 Standard `gen_ai.evaluation.result` event

| Attribute | Value |
| --- | --- |
| `gen_ai.evaluation.name` | `dpk_gate` |
| `gen_ai.evaluation.score.value` | `1.0` pass, `0.0` fail |
| `gen_ai.evaluation.score.label` | `pass` \| `fail` |
| `gen_ai.evaluation.explanation` | One-line verdict summary (truncated, §3.1) |

Upstream says this event SHOULD be parented to the GenAI operation span it evaluates. A
gate evaluates a whole run, so the right parent is the run's root (agent or workflow)
span, which is what §3.4 produces.

### 3.4 Placement

- **On an existing span** (`attach_verdict`): attributes and both events are added; the
  span status is never changed.
- **Dedicated span** (`record_verdict_span`, `verdict_to_otlp_json`, `dpk gate
  --otlp-out`): a `SPAN_KIND_INTERNAL` span named `dpk.gate`. Its status is `ERROR`
  with the summary as description when the gate failed (configurable), else `UNSET`.
  For OTLP output the trace id defaults to the candidate run id, which re-joins the
  trace the candidate was ingested from (§1).

### 3.5 Vendor-native forms

Some backends surface evaluations through their own APIs rather than span attributes.
The Python adapters map the same verdict as follows. None of these are part of the
vector contract.

| Backend | API | Mapping |
| --- | --- | --- |
| LangSmith | `Client.create_feedback` | key `dpk_gate`, `score` 1/0, `value` pass/fail, `comment` summary, `source_info` = §3.2 attributes |
| Langfuse | `create_score` (v3+) / `score` (v2) | name `dpk_gate`, `data_type` `BOOLEAN`, value 1/0, `comment` summary, `metadata` = §3.2 attributes |
| Arize Phoenix | `Client().spans.add_span_annotation` | name `dpk_gate`, `annotator_kind` `CODE`, label pass/fail, score 1/0, explanation summary, metadata = §3.2 attributes |
| Datadog LLM Observability | `LLMObs.submit_evaluation` | label `dpk_gate`, `metric_type` `categorical`, value pass/fail, tags `dpk.gate.verdict` / `dpk.gate.regression_level` / `dpk.candidate.run_id`, plus `assessment`, `reasoning`, `metadata` where the installed ddtrace supports them |

## 4. Conformance

An SDK conforms to this mapping's export side when, for every case in
`vectors/otel_verdict_attributes.json`:

1. building the verdict from `verdict` (or from `gate_json`) and flattening it yields
   exactly `attributes`, after removing the keys listed in `excluded_keys`, which MUST
   still be present as strings;
2. its OTLP/JSON encoding equals `otlp_attributes`, ignoring order (`int64` as a decimal
   string, `boolean` as `boolValue`, `double` as `doubleValue`, `string[]` as
   `arrayValue`);
3. its `gen_ai.evaluation.result` attributes equal `evaluation_result`; and
4. `label` and `score` match.

A proposal for standardizing evidence and verdict attributes upstream is drafted in
[`proposals/OTEL_GENAI_EVIDENCE_ATTRIBUTES_DRAFT.md`](proposals/OTEL_GENAI_EVIDENCE_ATTRIBUTES_DRAFT.md).
It has not been submitted.

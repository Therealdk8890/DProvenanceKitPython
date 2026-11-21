# Send gate verdicts to the observability stack you already use

DProvenanceKit does not ask you to adopt another dashboard. The gate decides whether an
agent's decision path regressed; `dprovenancekit.exporters` puts that verdict next to
the traces your team already reads in LangSmith, Langfuse, Arize Phoenix, Datadog, or
any OpenTelemetry backend.

Every route carries the same facts: pass/fail, severity, which steps were removed or
added, the golden and candidate run ids and fingerprints, the policy that was applied,
and optional evidence references such as an attestation digest. The attribute contract
is [`conformance/OTEL_GENAI_MAPPING_v1.md`](../conformance/OTEL_GENAI_MAPPING_v1.md) §3.

| You use | Recommended route | Extra to install |
| --- | --- | --- |
| Any OTel backend, from CI with no SDK | `dpk gate --otlp-out verdict.json`, then POST it | none |
| An app already instrumented with OpenTelemetry | `record_verdict_span` / `attach_verdict` | `[otel]` (only for error status) |
| LangSmith | `send_verdict_to_langsmith` (run feedback) | `[langsmith]` |
| Langfuse | `send_verdict_to_langfuse` (score), or the OTLP route | `[langfuse]` |
| Arize Phoenix | `send_verdict_to_phoenix` (span annotation), or the OTel tracer route | `[phoenix]` |
| Datadog LLM Observability | `send_verdict_to_datadog` (evaluation) | `[datadog]` |

All vendor SDKs are optional and imported only when an adapter is called without your
own client object. Adapters only send keyword arguments that the installed SDK version
declares, so they keep working across SDK upgrades.

## 0. Start from the traces you already have

You do not need to instrument anything to get a first gate. Export one known-good trace
and pin it as the golden baseline:

```bash
# OTLP/HTTP JSON (an OTel collector file exporter, Langfuse/Phoenix export, ...)
dpk init --from-otel traces.json
# or LangSmith runs exported as JSON / JSON-lines (one trace's runs, or many traces)
dpk init --from-langsmith runs.jsonl
```

`dpk init` imports every trace into `.dprovenance/traces.sqlite`, pins the newest one
(or `--golden-trace <trace id>`) as `.dprovenance/baseline.sqlite`, and prints the two
commands that gate a later trace:

```bash
dpk ingest new-trace.json --db .dprovenance/traces.sqlite            # --format langsmith for LangSmith runs
dpk gate --baseline .dprovenance/baseline.sqlite --candidate-db .dprovenance/traces.sqlite
```

Imported run ids are the source trace ids (OTel trace id, LangSmith trace id), so the
verdict can be sent back to the same trace with the routes below. LangSmith run types
map onto OpenInference span kinds; see `dprovenancekit.langsmith_import`. All files given
to one `dpk init` are read as a single batch, so a trace split across rotated collector
files (or LangSmith parents and children in different exports) is imported whole. LangSmith
traces that still contain an unfinished run are skipped with a warning; export them again
once they complete.

## 1. Build a verdict

In-process, from a `RegressionReport`:

```python
from dprovenancekit import RegressionGate
from dprovenancekit.exporters import GateVerdict

report = RegressionGate().check(golden_run, candidate_run)
verdict = GateVerdict.from_report(
    report,
    golden_run_id=golden_run.run_id,
    candidate_run_id=candidate_run.run_id,
    profile="strict_audit_v1",
    evidence={"attestation.format": "DPK-BINARY-V1", "attestation.sha256": digest},
)
```

In CI, from the gate's JSON output (`golden_run_id`, `candidate_run_id` and `profile`
are included from this release on; older output still works):

```bash
dpk gate --db traces.sqlite --golden "$GOLDEN" --candidate "$CANDIDATE" --json > gate.json || true
```

```python
import json
from dprovenancekit.exporters import GateVerdict

verdict = GateVerdict.from_gate_json(json.load(open("gate.json")))
```

## 2. OpenTelemetry

### From CI, no SDK required

```bash
dpk gate --db traces.sqlite --golden "$GOLDEN" --candidate "$CANDIDATE" \
  --otlp-out verdict.otlp.json
```

`verdict.otlp.json` is an OTLP/HTTP JSON `ExportTraceServiceRequest` with one `dpk.gate`
span. When the candidate run was imported from an OTel trace (`dpk ingest` or
`dpk init --from-otel`), the span's trace id is that trace's id, so the verdict appears
inside the original trace. Send it to any OTLP/HTTP endpoint that accepts JSON, for
example an OpenTelemetry Collector, or Langfuse directly:

```bash
curl -X POST "$LANGFUSE_HOST/api/public/otel/v1/traces" \
  -u "$LANGFUSE_PUBLIC_KEY:$LANGFUSE_SECRET_KEY" \
  -H "Content-Type: application/json" \
  -H "x-langfuse-ingestion-version: 4" \
  --data-binary @verdict.otlp.json
```

If a backend only accepts OTLP protobuf, put an OpenTelemetry Collector in front of it
(OTLP/JSON in, the backend's exporter out).

### From an instrumented app or test

```python
from opentelemetry import trace
from dprovenancekit.exporters import attach_verdict, record_verdict_span

tracer = trace.get_tracer("my-agent-ci")
record_verdict_span(tracer, verdict)            # a child `dpk.gate` span
attach_verdict(trace.get_current_span(), verdict)  # or annotate an existing span
```

Both add `dpk.*` attributes, a `dpk.gate.verdict` event, and the standard GenAI
`gen_ai.evaluation.result` event (`gen_ai.evaluation.name = dpk_gate`, score 1/0, label
pass/fail). Backends that understand GenAI evaluation events show the verdict even if
they know nothing about DProvenanceKit. `record_verdict_span` marks a failed gate with
status `ERROR`; `attach_verdict` never changes the status of your span.

## 3. LangSmith

```python
from dprovenancekit.exporters.langsmith import send_verdict_to_langsmith

send_verdict_to_langsmith(verdict, run_id=langsmith_run_id)  # langsmith.Client() from env
```

The verdict is stored as feedback with key `dpk_gate` (score 1/0, value pass/fail, the
summary as the comment, all `dpk.*` attributes as `source_info`). If the candidate was
imported with `dpk init --from-langsmith`, its DPK run id is the LangSmith trace id, so
`run_id` can be omitted. Pass `project_id=` instead to attach project-level feedback
(it cannot be combined with `run_id` or `trace_id`).

## 4. Langfuse

```python
from langfuse import get_client
from dprovenancekit.exporters.langfuse import send_verdict_to_langfuse

langfuse = get_client()
send_verdict_to_langfuse(verdict, trace_id=langfuse_trace_id, client=langfuse)
langfuse.flush()  # short-lived CI processes must flush before exiting
```

A `BOOLEAN` score named `dpk_gate` on the trace (or on one observation with
`observation_id=`), with the summary as the comment and the `dpk.*` attributes as
metadata. Works with `create_score` (SDK v3 and later) and the legacy v2 `score`.

## 5. Arize Phoenix

```python
from dprovenancekit.exporters.phoenix import send_verdict_to_phoenix

send_verdict_to_phoenix(verdict, span_id=root_span_id)  # phoenix.client.Client() from env
```

A `CODE` span annotation named `dpk_gate` (label pass/fail, score 1/0, the summary as
the explanation). For Phoenix's attribute view, also emit the verdict span with
`record_verdict_span` on the tracer provider you registered with
`phoenix.otel.register()`.

## 6. Datadog LLM Observability

```python
from ddtrace.llmobs import LLMObs
from dprovenancekit.exporters.datadog import send_verdict_to_datadog

span_ref = LLMObs.export_span()   # inside the traced agent operation
send_verdict_to_datadog(verdict, span=span_ref)
```

A `categorical` evaluation labeled `dpk_gate` with value pass/fail, tagged
`dpk.gate.verdict`, `dpk.gate.regression_level` and `dpk.candidate.run_id`. To join on a
tag instead of a span reference, pass
`span_with_tag_value={"tag_key": "...", "tag_value": "..."}`. LLM Observability must be
enabled, or ddtrace drops the evaluation.

## What this does not do

- It does not upload traces anywhere by itself. Each adapter makes exactly one call
  you invoke, with the client you configure.
- It does not replace the gate's exit code. CI should still fail on `dpk gate`'s exit
  status; exporting the verdict is for visibility.
- It does not make a verdict a claim about output correctness. A pass means the
  candidate followed the same decision path as the golden run under the configured
  policy.

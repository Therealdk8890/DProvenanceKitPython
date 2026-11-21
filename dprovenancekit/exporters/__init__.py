"""Integrate, don't replace: put DProvenanceKit gate verdicts where teams already look.

The gate decides whether an agent's decision path regressed. This package carries that
verdict into the observability stack a team already runs, instead of asking them to
adopt another dashboard:

* :mod:`~dprovenancekit.exporters.otel`: ``dpk.*`` span attributes and a
  ``dpk.gate.verdict`` span event on any OpenTelemetry span or tracer, plus a
  stdlib-only OTLP/HTTP JSON encoder for CI jobs without an OTel SDK.
* :mod:`~dprovenancekit.exporters.langsmith`: LangSmith run feedback.
* :mod:`~dprovenancekit.exporters.langfuse`: Langfuse scores.
* :mod:`~dprovenancekit.exporters.phoenix`: Arize Phoenix span annotations.
* :mod:`~dprovenancekit.exporters.datadog`: Datadog LLM Observability evaluations.

Every adapter takes a :class:`GateVerdict`, built from an in-process
:class:`~dprovenancekit.testing.RegressionReport` or from ``dpk gate --json`` output.
Vendor SDKs are optional extras and are imported lazily, only when you call an adapter
without passing your own client. The attribute contract is specified in
``conformance/OTEL_GENAI_MAPPING_v1.md``.
"""

from .otel import attach_verdict, record_verdict_span, verdict_to_otlp_json
from .verdict import VERDICT_SCHEMA, GateVerdict

__all__ = [
    "GateVerdict",
    "VERDICT_SCHEMA",
    "attach_verdict",
    "record_verdict_span",
    "verdict_to_otlp_json",
]

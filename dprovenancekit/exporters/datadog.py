"""Send a DProvenanceKit gate verdict to Datadog LLM Observability as an evaluation.

Uses the documented ``ddtrace.llmobs.LLMObs.submit_evaluation`` API: the verdict becomes
a ``categorical`` evaluation (default label ``dpk_gate``, value ``pass``/``fail``) joined
to an LLM Observability span, tagged with the gate verdict and severity. On ddtrace
versions that support them, ``assessment``, ``reasoning`` and ``metadata`` carry the
pass/fail assessment, the verdict summary and the ``dpk.*`` attributes.

    from ddtrace.llmobs import LLMObs
    from dprovenancekit.exporters.datadog import send_verdict_to_datadog

    span_ref = LLMObs.export_span()          # inside the traced operation
    send_verdict_to_datadog(verdict, span=span_ref)

LLM Observability must be enabled (``LLMObs.enable(...)`` or ``DD_LLMOBS_ENABLED=1``);
otherwise ddtrace drops the evaluation. For APM span attributes instead, use
:func:`dprovenancekit.exporters.otel.attach_verdict` with ddtrace's OpenTelemetry API
support.

Requires ``pip install "dprovenancekit[datadog]"`` unless you pass ``llmobs``.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from ._util import call_supported, json_metadata, missing_extra
from .verdict import GateVerdict

DEFAULT_EVALUATION_LABEL = "dpk_gate"


def _default_llmobs() -> Any:
    try:
        from ddtrace.llmobs import LLMObs
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise missing_extra("ddtrace", "datadog") from exc
    return LLMObs


def verdict_tags(verdict: GateVerdict) -> Dict[str, str]:
    """String tags Datadog can facet on (tags must be ``str`` → ``str``)."""
    tags = {"dpk.gate.verdict": verdict.label}
    if verdict.regression_level is not None:
        tags["dpk.gate.regression_level"] = verdict.regression_level
    if verdict.candidate_run_id is not None:
        tags["dpk.candidate.run_id"] = verdict.candidate_run_id
    return tags


def send_verdict_to_datadog(
    verdict: GateVerdict,
    span: Optional[Mapping[str, str]] = None,
    *,
    span_with_tag_value: Optional[Mapping[str, str]] = None,
    llmobs: Optional[Any] = None,
    label: str = DEFAULT_EVALUATION_LABEL,
    tags: Optional[Mapping[str, str]] = None,
) -> Any:
    """Submit ``verdict`` as a Datadog LLM Observability evaluation.

    Args:
        verdict: the gate verdict.
        span: ``{"span_id": ..., "trace_id": ...}`` as returned by
            ``LLMObs.export_span()``.
        span_with_tag_value: alternatively ``{"tag_key": ..., "tag_value": ...}`` to
            join on a span tag (for example a tag holding the DPK run id).
        llmobs: the ``LLMObs`` class/instance (default ``ddtrace.llmobs.LLMObs``).
        label: evaluation label (default ``dpk_gate``).
        tags: extra evaluation tags merged over the verdict tags.
    """
    if (span is None) == (span_with_tag_value is None):
        raise ValueError("provide exactly one of span or span_with_tag_value")
    llmobs = llmobs if llmobs is not None else _default_llmobs()
    merged = verdict_tags(verdict)
    if tags:
        merged.update({str(k): str(v) for k, v in tags.items()})
    return call_supported(
        llmobs.submit_evaluation,
        label=label,
        metric_type="categorical",
        value=verdict.label,
        span=dict(span) if span is not None else None,
        span_with_tag_value=(
            dict(span_with_tag_value) if span_with_tag_value is not None else None
        ),
        tags=merged,
        metadata=json_metadata(verdict.to_attributes()),
        assessment=verdict.label,
        reasoning=verdict.summary(),
    )


__all__ = ["DEFAULT_EVALUATION_LABEL", "send_verdict_to_datadog", "verdict_tags"]

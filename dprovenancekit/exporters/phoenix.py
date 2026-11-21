"""Send a DProvenanceKit gate verdict to Arize Phoenix as a span annotation.

Uses the documented ``arize-phoenix-client`` API
``Client().spans.add_span_annotation``: the verdict becomes a ``CODE`` annotation
(default name ``dpk_gate``) with label ``pass``/``fail``, score 1/0, the verdict summary
as the explanation and the ``dpk.*`` attributes as metadata.

    from dprovenancekit.exporters.phoenix import send_verdict_to_phoenix

    send_verdict_to_phoenix(verdict, span_id="<16-hex span id>")

For Phoenix's span-attribute view, also (or instead) emit the verdict through your OTel
tracer with :func:`dprovenancekit.exporters.otel.record_verdict_span`; Phoenix ingests
OTLP natively.

Requires ``pip install "dprovenancekit[phoenix]"`` unless you pass ``client``.
"""

from __future__ import annotations

from typing import Any, Optional

from ._util import call_supported, json_metadata, missing_extra
from .verdict import GateVerdict

DEFAULT_ANNOTATION_NAME = "dpk_gate"


def _default_client() -> Any:
    try:
        from phoenix.client import Client
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise missing_extra("arize-phoenix-client", "phoenix") from exc
    return Client()


def send_verdict_to_phoenix(
    verdict: GateVerdict,
    span_id: str,
    *,
    client: Optional[Any] = None,
    name: str = DEFAULT_ANNOTATION_NAME,
    identifier: Optional[str] = None,
    sync: bool = False,
) -> Any:
    """Annotate Phoenix span ``span_id`` with ``verdict``; return the API result.

    Args:
        verdict: the gate verdict.
        span_id: the OpenTelemetry span id (16 hex characters) to annotate.
        client: a configured ``phoenix.client.Client`` (default reads
            ``PHOENIX_COLLECTOR_ENDPOINT`` / ``PHOENIX_API_KEY``).
        name: annotation name (default ``dpk_gate``).
        identifier: optional idempotency identifier; re-sending with the same
            identifier updates the annotation instead of adding another.
        sync: wait for the server to confirm the insert.
    """
    if not span_id:
        raise ValueError("span_id is required")
    client = client if client is not None else _default_client()
    return call_supported(
        client.spans.add_span_annotation,
        span_id=span_id,
        annotation_name=name,
        annotator_kind="CODE",
        label=verdict.label,
        score=verdict.score,
        explanation=verdict.summary(),
        metadata=json_metadata(verdict.to_attributes()),
        identifier=identifier,
        sync=sync,
    )


__all__ = ["DEFAULT_ANNOTATION_NAME", "send_verdict_to_phoenix"]

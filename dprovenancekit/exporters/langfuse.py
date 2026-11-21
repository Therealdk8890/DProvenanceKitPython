"""Send a DProvenanceKit gate verdict to Langfuse as a score.

Uses the documented Langfuse Python SDK scoring API: ``create_score`` (SDK v3 and
later) or, on the legacy v2 SDK, ``score``. The verdict becomes a ``BOOLEAN`` score
(default name ``dpk_gate``, value ``1`` pass / ``0`` fail) on a trace, or on one
observation within it, with the verdict summary as ``comment`` and the ``dpk.*``
attributes as ``metadata`` where the SDK supports it.

    from dprovenancekit.exporters.langfuse import send_verdict_to_langfuse

    send_verdict_to_langfuse(verdict, trace_id="<langfuse trace id>")

Requires ``pip install "dprovenancekit[langfuse]"`` unless you pass ``client``.
"""

from __future__ import annotations

from typing import Any, Optional

from ._util import call_supported, hex32_or_none, json_metadata, missing_extra
from .verdict import GateVerdict

DEFAULT_SCORE_NAME = "dpk_gate"


def _default_client() -> Any:
    try:
        import langfuse
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise missing_extra("langfuse", "langfuse") from exc
    get_client = getattr(langfuse, "get_client", None)
    if get_client is not None:  # v3+: the singleton configured from LANGFUSE_* env vars
        return get_client()
    return langfuse.Langfuse()  # pragma: no cover - legacy v2


def send_verdict_to_langfuse(
    verdict: GateVerdict,
    trace_id: Optional[str] = None,
    *,
    observation_id: Optional[str] = None,
    client: Optional[Any] = None,
    name: str = DEFAULT_SCORE_NAME,
) -> Any:
    """Record ``verdict`` as a Langfuse score; return whatever the SDK returns.

    Args:
        verdict: the gate verdict.
        trace_id: the Langfuse trace id. Defaults to the verdict's ``candidate_run_id``
            rendered as 32 hex characters (the OTel trace id it was imported from).
        observation_id: optionally score one observation (span) instead of the trace.
        client: a configured Langfuse client (default: ``langfuse.get_client()``).
        name: score name (default ``dpk_gate``).

    The score is queued by the Langfuse SDK; call ``client.flush()`` before a
    short-lived CI process exits.
    """
    target = trace_id or hex32_or_none(verdict.candidate_run_id)
    if not target:
        raise ValueError(
            "trace_id is required (the verdict has no UUID candidate_run_id to default to)"
        )
    client = client if client is not None else _default_client()
    create_score = getattr(client, "create_score", None)
    method = create_score if create_score is not None else getattr(client, "score", None)
    if method is None:
        raise TypeError("client has neither create_score (Langfuse v3+) nor score (v2)")
    return call_supported(
        method,
        name=name,
        value=verdict.score,
        trace_id=target,
        observation_id=observation_id,
        data_type="BOOLEAN",
        comment=verdict.summary(),
        metadata=json_metadata(verdict.to_attributes()),
    )


__all__ = ["DEFAULT_SCORE_NAME", "send_verdict_to_langfuse"]

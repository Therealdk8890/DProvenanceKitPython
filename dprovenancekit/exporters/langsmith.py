"""Send a DProvenanceKit gate verdict to LangSmith as run feedback.

Uses the documented ``langsmith.Client.create_feedback`` API: the verdict becomes a
feedback entry (default key ``dpk_gate``) with ``score`` 1/0, ``value`` ``"pass"`` /
``"fail"``, the verdict summary as ``comment`` and the full ``dpk.*`` attribute map as
``source_info``, so it shows up next to LangSmith's own evaluators and can be filtered
and charted the same way.

    from dprovenancekit.exporters import GateVerdict
    from dprovenancekit.exporters.langsmith import send_verdict_to_langsmith

    send_verdict_to_langsmith(verdict, run_id="<langsmith run id>")

When the candidate run was imported with ``dpk init --from-langsmith``, its DPK run id
equals the LangSmith trace id (the root run id), so ``run_id`` can be omitted.

Requires ``pip install "dprovenancekit[langsmith]"`` unless you pass ``client``.
"""

from __future__ import annotations

from typing import Any, Optional

from ._util import call_supported, json_metadata, missing_extra, uuid_or_none
from .verdict import GateVerdict

DEFAULT_FEEDBACK_KEY = "dpk_gate"


def _default_client() -> Any:
    try:
        from langsmith import Client
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise missing_extra("langsmith", "langsmith") from exc
    return Client()


def send_verdict_to_langsmith(
    verdict: GateVerdict,
    run_id: Optional[Any] = None,
    *,
    client: Optional[Any] = None,
    key: str = DEFAULT_FEEDBACK_KEY,
    trace_id: Optional[Any] = None,
    project_id: Optional[Any] = None,
) -> Any:
    """Record ``verdict`` as LangSmith feedback on ``run_id``; return the API result.

    Args:
        verdict: the gate verdict.
        run_id: the LangSmith run to attach feedback to. Defaults to the verdict's
            ``candidate_run_id``.
        client: a configured ``langsmith.Client`` (default: ``langsmith.Client()``,
            which reads ``LANGSMITH_API_KEY`` / ``LANGSMITH_ENDPOINT``).
        key: feedback key (default ``dpk_gate``).
        trace_id: optional trace id, forwarded when the installed SDK supports it.
        project_id: optional project id, forwarded when the installed SDK supports it.
    """
    target = run_id if run_id is not None else uuid_or_none(verdict.candidate_run_id)
    if target is None:
        raise ValueError(
            "run_id is required (the verdict has no UUID candidate_run_id to default to)"
        )
    client = client if client is not None else _default_client()
    return call_supported(
        client.create_feedback,
        target,
        key,
        score=verdict.score,
        value=verdict.label,
        comment=verdict.summary(),
        source_info=json_metadata(verdict.to_attributes()),
        trace_id=trace_id,
        project_id=project_id,
    )


__all__ = ["DEFAULT_FEEDBACK_KEY", "send_verdict_to_langsmith"]

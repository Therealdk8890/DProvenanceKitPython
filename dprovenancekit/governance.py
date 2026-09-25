"""Bridge controller governance events into DProvenanceKit traces.

The bridge accepts a mapping rather than importing AgentContainment. This keeps
DProvenanceKit a standalone provenance SDK while providing a stable integration
point for controller-owned governance events.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional
from uuid import UUID
import math

from .instrument import record_event
from .priority import TracePriority


def record_governance_event(
    event: Mapping[str, Any],
    *,
    priority: TracePriority = TracePriority.STRUCTURAL,
) -> Optional[UUID]:
    """Record one controller governance event in the active provenance run.

    The controller remains authoritative. This function only records the fact
    that the event occurred; it never authorizes, contains, or recovers an
    agent. Envelope fields are preserved as trace attributes so downstream
    systems can correlate the governance event with its source controller.
    """
    required = ("event_id", "event_type", "timestamp", "agent_id")
    missing = [key for key in required if key not in event]
    for key in required:
        if key in event and key != "timestamp" and (not isinstance(event[key], str) or not event[key]):
            raise ValueError(f"{key} must be a non-empty string")
    if "timestamp" in event and (isinstance(event["timestamp"], bool) or not isinstance(event["timestamp"], (int, float))):
        raise ValueError("timestamp must be numeric")
    if "timestamp" in event and not math.isfinite(event["timestamp"]):
        raise ValueError("timestamp must be finite")
    if missing:
        raise ValueError(f"governance event missing required fields: {', '.join(missing)}")

    event_type = event["event_type"]
    if not isinstance(event_type, str) or not event_type:
        raise ValueError("event_type must be a non-empty string")

    attributes = dict(event)
    attributes.pop("event_type", None)
    nested = attributes.get("attributes")
    if isinstance(nested, Mapping):
        attributes.pop("attributes")
        attributes.update({f"attribute.{key}": value for key, value in nested.items()})

    return record_event(
        f"agent_containment.{event_type}",
        attributes,
        priority=priority,
    )


__all__ = ["record_governance_event"]

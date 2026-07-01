"""Provenance edges between trace events."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import Enum


class TraceEdgeType(str, Enum):
    DERIVED_FROM = "derivedFrom"
    INFLUENCED_BY = "influencedBy"
    GENERATED_FROM = "generatedFrom"
    VERIFIED_BY = "verifiedBy"
    CORRECTED_BY = "correctedBy"
    INFORMED = "informed"


@dataclass(frozen=True)
class TraceEdge:
    source_id: uuid.UUID
    target_id: uuid.UUID
    type: TraceEdgeType


"""Presentation hints for trace viewers."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Set


class DiffPresentationMode(Enum):
    NONE = "none"
    SINGLE_SNAPSHOT = "singleSnapshot"
    COMPARISON = "comparison"


@dataclass(frozen=True)
class RenderHints:
    collapsed_by_default: Set[str] = field(default_factory=set)
    important_event_types: Set[str] = field(default_factory=set)
    highlight_quarantine: bool = True
    diff_mode: DiffPresentationMode = DiffPresentationMode.NONE


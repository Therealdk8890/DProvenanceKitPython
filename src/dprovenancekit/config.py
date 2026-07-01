"""Buffering and eviction configuration for offline (local) trace storage."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from enum import Enum

_MAX_INT = sys.maxsize


@dataclass(frozen=True)
class BufferCapacity:
    max_items: int = 50_000
    max_bytes: int = 50 * 1024 * 1024
    max_event_size_bytes: int = 1 * 1024 * 1024


class EvictionPolicy(Enum):
    DROP_OLDEST = "dropOldest"
    REJECT_NEW = "rejectNew"


@dataclass(frozen=True)
class OfflineConfig:
    capacity: BufferCapacity = BufferCapacity()
    eviction: EvictionPolicy = EvictionPolicy.DROP_OLDEST

# git-blob-rewrite

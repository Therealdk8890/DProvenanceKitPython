"""Priority tiers for trace events.

Determines congestion control and sampling behavior under extreme load. Mirrors the
Swift ``TracePriority`` enum: the raw integer values are part of the contract (used as
indices into per-tier buffers and drop tallies), and the ordering is meaningful.
"""

from __future__ import annotations

from enum import IntEnum


class TracePriority(IntEnum):
    """Priority tiers, ordered from most droppable to never-dropped.

    ``IntEnum`` gives us the same ``<`` / ``>=`` comparison semantics the Swift
    ``Comparable`` conformance provides, and the raw values double as buffer indices.
    """

    #: Purely quantitative / high-frequency signals (intermediate token counts, debug
    #: stats). MUST NEVER affect reasoning correctness or diff results. Dropped first.
    TELEMETRY = 0

    #: Qualitative debugging state. Useful for debugging but not strictly necessary for
    #: logical verification.
    DIAGNOSTIC = 1

    #: Execution logic integrity. Essential for preserving logical structure and
    #: sequence. Capped per run under extreme load but preserved globally if possible.
    STRUCTURAL = 2

    #: Replay correctness boundary. Crucial for replay integrity and anomaly detection.
    #: NEVER dropped (e.g. start, end, error).
    CRITICAL = 3

# git-blob-rewrite

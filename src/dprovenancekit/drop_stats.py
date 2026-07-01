"""By-tier accounting of events shed under congestion or lost outside the buffer.

A diff or query is only as honest as the data behind it. Silent shedding is the
difference between "impressive" and "trustworthy": :attr:`TraceDropStats.preserved_integrity`
collapses the by-tier breakdown into the one bit a caller usually wants — whether
anything a structural diff depends on was lost.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from .priority import TracePriority


@dataclass(frozen=True)
class TraceDropStats:
    """A by-tier tally of trace events that were intentionally shed."""

    telemetry: int = 0
    diagnostic: int = 0
    structural: int = 0
    critical: int = 0

    @property
    def total(self) -> int:
        """Total events shed across every tier."""
        return self.telemetry + self.diagnostic + self.structural + self.critical

    @property
    def preserved_integrity(self) -> bool:
        """True when nothing that can change a structural diff was dropped.

        Telemetry and diagnostic events never participate in a structural diff, so
        shedding them leaves diff/query integrity intact. Only a structural or critical
        drop can make two genuinely-different runs look identical.
        """
        return self.structural == 0 and self.critical == 0

    def __getitem__(self, priority: TracePriority) -> int:
        return {
            TracePriority.TELEMETRY: self.telemetry,
            TracePriority.DIAGNOSTIC: self.diagnostic,
            TracePriority.STRUCTURAL: self.structural,
            TracePriority.CRITICAL: self.critical,
        }[priority]

    def __add__(self, other: "TraceDropStats") -> "TraceDropStats":
        """Tier-wise sum, so drops counted in different places combine into one total."""
        return TraceDropStats(
            telemetry=self.telemetry + other.telemetry,
            diagnostic=self.diagnostic + other.diagnostic,
            structural=self.structural + other.structural,
            critical=self.critical + other.critical,
        )


TraceDropStats.zero = TraceDropStats()  # type: ignore[attr-defined]


class TraceDropTally:
    """A thread-safe, by-tier tally of events lost *outside* the write buffer.

    The buffer counts its own congestion shedding; this counts the other store-level
    loss sites (encode failures, failed batch inserts) so neither vanishes silently
    while ``preserved_integrity`` still claims everything was retained. A single
    instance is shared by reference between a store and its writer.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_tier = [0, 0, 0, 0]

    def record(self, priority: int, count: int = 1) -> None:
        """Record ``count`` lost events in the given priority tier.

        Out-of-range tiers are ignored rather than raising — a tally must never be the
        thing that crashes.
        """
        with self._lock:
            if 0 <= priority < len(self._by_tier):
                self._by_tier[priority] += count

    @property
    def snapshot(self) -> TraceDropStats:
        """A point-in-time snapshot of everything tallied so far."""
        with self._lock:
            return TraceDropStats(
                telemetry=self._by_tier[TracePriority.TELEMETRY],
                diagnostic=self._by_tier[TracePriority.DIAGNOSTIC],
                structural=self._by_tier[TracePriority.STRUCTURAL],
                critical=self._by_tier[TracePriority.CRITICAL],
            )

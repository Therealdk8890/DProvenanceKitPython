"""Priority-aware, zero-blocking write buffer.

Backed by a lock so ``enqueue`` is synchronous: an event is in the buffer the instant
``record`` returns, giving callers a real happens-before guarantee against ``flush``.
Congestion control is priority-bucketed (one FIFO per tier) so both ingestion and
load-shedding stay O(1) even when a burst pins the buffer at capacity. Draining performs
a k-way merge across the tiers so events still reach the writer in global insertion order.
"""

from __future__ import annotations

import sys
import threading
from dataclasses import dataclass
from typing import Dict, List, Optional

from .config import BufferCapacity, EvictionPolicy, OfflineConfig
from .drop_stats import TraceDropStats
from .edge import TraceEdge
from .event import TraceEventRow
from .priority import TracePriority

_MAX_INT = sys.maxsize


class _FIFOQueue:
    """Amortized-O(1) FIFO backed by a list with a moving head cursor."""

    __slots__ = ("_storage", "_head")

    def __init__(self) -> None:
        self._storage: List = []
        self._head = 0

    @property
    def count(self) -> int:
        return len(self._storage) - self._head

    @property
    def first(self):
        if self._head < len(self._storage):
            return self._storage[self._head]
        return None

    def append(self, element) -> None:
        self._storage.append(element)

    def pop_first(self):
        if self._head >= len(self._storage):
            return None
        element = self._storage[self._head]
        self._head += 1
        # Reclaim the dead prefix once it dominates, amortizing to O(1) per pop.
        if self._head > 1024 and self._head * 2 >= len(self._storage):
            del self._storage[: self._head]
            self._head = 0
        return element


@dataclass(frozen=True)
class _Buffered:
    stamp: int
    row: TraceEventRow
    bytes: int


class TraceWriteBuffer:
    """A buffer that queues trace events in memory to provide a zero-blocking write path."""

    def __init__(
        self,
        max_global_buffer: Optional[int] = None,
        max_per_run_buffer: int = 5_000,
        config: Optional[OfflineConfig] = None,
    ) -> None:
        if config is not None:
            self._config = config
        elif max_global_buffer is not None:
            self._config = OfflineConfig(
                capacity=BufferCapacity(
                    max_items=max_global_buffer,
                    max_bytes=_MAX_INT,
                    max_event_size_bytes=_MAX_INT,
                ),
                eviction=EvictionPolicy.DROP_OLDEST,
            )
        else:
            self._config = OfflineConfig()

        self._max_per_run_buffer = max_per_run_buffer
        self._lock = threading.Lock()
        self._tiers: List[_FIFOQueue] = [_FIFOQueue() for _ in range(4)]
        self._edge_queue = _FIFOQueue()
        self._total_count = 0
        self._total_bytes = 0
        self._enqueue_counter = 0
        self._dropped_by_tier = [0, 0, 0, 0]
        self._queue_depth_by_run: Dict[str, int] = {}

    @property
    def current_depth(self) -> int:
        with self._lock:
            return self._total_count

    @property
    def drop_stats(self) -> TraceDropStats:
        with self._lock:
            return TraceDropStats(
                telemetry=self._dropped_by_tier[TracePriority.TELEMETRY],
                diagnostic=self._dropped_by_tier[TracePriority.DIAGNOSTIC],
                structural=self._dropped_by_tier[TracePriority.STRUCTURAL],
                critical=self._dropped_by_tier[TracePriority.CRITICAL],
            )

    def enqueue(self, event: TraceEventRow) -> None:
        """Enqueue an event using priority-aware congestion control. O(1) on both paths."""
        with self._lock:
            try:
                priority = TracePriority(event.priority)
            except ValueError:
                priority = TracePriority.TELEMETRY
            event_bytes = len(event.payload) + 256
            cap = self._config.capacity

            if event_bytes > cap.max_event_size_bytes:
                self._dropped_by_tier[priority] += 1
                return

            run_depth = self._queue_depth_by_run.get(event.run_id, 0)

            # 1. Soft per-run limit: shed verbose/diagnostic for a bursting run, but keep
            #    its structural and critical events even while it bursts.
            if run_depth >= self._max_per_run_buffer and priority <= TracePriority.DIAGNOSTIC:
                self._dropped_by_tier[priority] += 1
                return

            # 2. Global capacity: evict the lowest-priority, oldest victim to make room.
            def over_capacity() -> bool:
                return (
                    self._total_count >= cap.max_items
                    or self._total_bytes + event_bytes > cap.max_bytes
                )

            while over_capacity():
                if self._config.eviction == EvictionPolicy.DROP_OLDEST:
                    if not self._evict_one_locked(priority):
                        self._dropped_by_tier[priority] += 1
                        return
                else:  # REJECT_NEW
                    self._dropped_by_tier[priority] += 1
                    return

            stamp = self._enqueue_counter
            self._enqueue_counter += 1
            self._tiers[priority].append(_Buffered(stamp=stamp, row=event, bytes=event_bytes))
            self._total_count += 1
            self._total_bytes += event_bytes
            self._queue_depth_by_run[event.run_id] = run_depth + 1

    def enqueue_edge(self, edge: TraceEdge) -> None:
        with self._lock:
            self._edge_queue.append(edge)

    def _evict_one_locked(self, incoming: TracePriority) -> bool:
        """Free one slot under global pressure. Returns False only when the incoming
        event should itself be dropped (nothing cheaper to discard)."""
        if self._pop_victim_locked(TracePriority.TELEMETRY):
            return True
        if self._pop_victim_locked(TracePriority.DIAGNOSTIC):
            return True
        # Only structural/critical remain. Preserve that backlog unless the incoming
        # event is critical.
        if incoming <= TracePriority.STRUCTURAL:
            return False
        if self._pop_victim_locked(TracePriority.STRUCTURAL):
            return True
        if self._pop_victim_locked(TracePriority.CRITICAL):
            return True
        return False

    def _pop_victim_locked(self, tier: TracePriority) -> bool:
        victim = self._tiers[tier].pop_first()
        if victim is None:
            return False
        self._total_count -= 1
        self._total_bytes -= victim.bytes
        self._dropped_by_tier[tier] += 1
        self._decrement_run_depth(victim.row.run_id)
        return True

    def _decrement_run_depth(self, run_id: str) -> None:
        current = self._queue_depth_by_run.get(run_id, 1) - 1
        if current == 0:
            self._queue_depth_by_run.pop(run_id, None)
        else:
            self._queue_depth_by_run[run_id] = current

    def drain(self, max: int = 1000) -> List[TraceEventRow]:
        with self._lock:
            return self._drain_locked(max)

    def flush_all(self) -> List[TraceEventRow]:
        with self._lock:
            return self._drain_locked(_MAX_INT)

    def _drain_locked(self, max_count: int) -> List[TraceEventRow]:
        """k-way merge across the priority tiers by insertion stamp."""
        if max_count <= 0 or self._total_count == 0:
            return []
        result: List[TraceEventRow] = []
        while len(result) < max_count:
            best_tier = -1
            best_stamp = _MAX_INT
            for t in range(len(self._tiers)):
                front = self._tiers[t].first
                if front is not None and front.stamp < best_stamp:
                    best_stamp = front.stamp
                    best_tier = t
            if best_tier < 0:
                break
            buffered = self._tiers[best_tier].pop_first()
            result.append(buffered.row)
            self._total_count -= 1
            self._total_bytes -= buffered.bytes
            self._decrement_run_depth(buffered.row.run_id)
        return result

    def drain_edges(self) -> List[TraceEdge]:
        with self._lock:
            result: List[TraceEdge] = []
            while True:
                edge = self._edge_queue.pop_first()
                if edge is None:
                    break
                result.append(edge)
            return result

# git-blob-rewrite

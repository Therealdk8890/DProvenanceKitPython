"""A simple thread-safe circuit breaker for the cloud writer."""

from __future__ import annotations

import threading
import time
from enum import Enum
from typing import Optional


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "halfOpen"


class CircuitBreaker:
    def __init__(self, max_failures: int = 5, decay_timeout: float = 30.0):
        self._lock = threading.Lock()
        self._state = CircuitState.CLOSED
        self._max_failures = max_failures
        self._decay_timeout = decay_timeout
        self._failure_count = 0
        self._last_failure_time: Optional[float] = None

    @property
    def state(self) -> CircuitState:
        with self._lock:
            return self._state

    def allow_request(self) -> bool:
        with self._lock:
            if self._state == CircuitState.CLOSED:
                return True
            if self._state == CircuitState.OPEN:
                if (
                    self._last_failure_time is not None
                    and time.time() - self._last_failure_time >= self._decay_timeout
                ):
                    self._state = CircuitState.HALF_OPEN
                    return True  # Allow one probe.
                return False
            # HALF_OPEN: only the initial probe is allowed; concurrent requests wait.
            return False

    def time_until_allowed(self) -> float:
        with self._lock:
            if self._state in (CircuitState.CLOSED, CircuitState.HALF_OPEN):
                return 0.0
            if self._last_failure_time is None:
                return 0.0
            elapsed = time.time() - self._last_failure_time
            return max(0.0, self._decay_timeout - elapsed)

    def record_success(self) -> None:
        with self._lock:
            self._failure_count = 0
            self._state = CircuitState.CLOSED
            self._last_failure_time = None

    def record_failure(self) -> None:
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.time()
            if self._state == CircuitState.CLOSED:
                if self._failure_count >= self._max_failures:
                    self._state = CircuitState.OPEN
            elif self._state == CircuitState.HALF_OPEN:
                self._state = CircuitState.OPEN


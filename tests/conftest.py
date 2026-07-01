"""Shared fixtures and the canonical ``TestEvent`` used across the suite.

Mirrors the Swift ``TestEvent`` defined in ``SQLiteStressTests.swift``: a four-case event
with a representative spread of priority tiers.
"""

from __future__ import annotations

import os
import sys
import tempfile
import uuid
from dataclasses import dataclass

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from dprovenancekit import TraceableEvent, TracePriority  # noqa: E402


@dataclass(frozen=True)
class TestEvent(TraceableEvent):
    # Tell pytest this is a domain type, not a test class to collect.
    __test__ = False

    kind: str  # processStarted | stepCompleted | errorDetected | processFinished
    value: int = 0

    @property
    def type_identifier(self) -> str:
        return self.kind

    @property
    def priority(self) -> TracePriority:
        if self.kind in ("processStarted", "processFinished"):
            return TracePriority.CRITICAL
        if self.kind == "errorDetected":
            return TracePriority.STRUCTURAL
        return TracePriority.TELEMETRY  # stepCompleted

    def to_dict(self) -> dict:
        return {"kind": self.kind, "value": self.value}

    @classmethod
    def from_dict(cls, data: dict) -> "TestEvent":
        return cls(kind=data["kind"], value=data.get("value", 0))

    # Factories mirroring the Swift enum cases.
    @classmethod
    def process_started(cls):
        return cls("processStarted")

    @classmethod
    def step_completed(cls, n: int):
        return cls("stepCompleted", value=n)

    @classmethod
    def error_detected(cls):
        return cls("errorDetected")

    @classmethod
    def process_finished(cls):
        return cls("processFinished")


@pytest.fixture
def temp_db_path(tmp_path):
    """An absolute path to a unique, not-yet-created SQLite file under pytest's tmp dir."""
    return str(tmp_path / (uuid.uuid4().hex + ".sqlite"))

# git-blob-rewrite

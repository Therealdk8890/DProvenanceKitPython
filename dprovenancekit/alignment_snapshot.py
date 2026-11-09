"""Snapshot the canonical render output of an alignment and validate against drift."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum
from typing import List

from .alignment_render import AlignmentRenderNode, render_models


@dataclass(frozen=True)
class AlignmentSnapshot:
    profile_hash: str
    engine_version: str
    output_alignments_hash: str


class DriftToleranceMode(Enum):
    STRICT = "strict"
    REPORT_ONLY = "reportOnly"


class SnapshotValidationError(Exception):
    def __init__(self, expected: str, actual: str, field: str = "output_alignments_hash"):
        super().__init__(f"{field} mismatch: expected {expected}, got {actual}")
        self.expected = expected
        self.actual = actual
        self.field = field


class AlignmentSnapshotValidator:
    def __init__(self, tolerance_mode: DriftToleranceMode = DriftToleranceMode.STRICT):
        self.tolerance_mode = tolerance_mode

    @staticmethod
    def compute_alignments_hash(render_nodes: List[AlignmentRenderNode]) -> str:
        full = "\n".join(n.canonical_serialization for n in render_nodes)
        return hashlib.sha256(full.encode("utf-8")).hexdigest()

    @staticmethod
    def create_snapshot(result) -> AlignmentSnapshot:
        nodes = render_models(result)
        return AlignmentSnapshot(
            profile_hash=result.profile_hash,
            engine_version=result.engine_version,
            output_alignments_hash=AlignmentSnapshotValidator.compute_alignments_hash(
                nodes
            ),
        )

    def _handle_mismatch(self, field: str, expected: str, actual: str) -> bool:
        if self.tolerance_mode == DriftToleranceMode.STRICT:
            raise SnapshotValidationError(expected, actual, field)
        print(
            f"⚠️ [AlignmentSnapshotValidator] {field} mismatch. "
            f"Expected {expected}, got {actual}"
        )
        return False

    def validate(self, result, snapshot: AlignmentSnapshot) -> bool:
        # Bind the snapshot to the exact profile and engine that produced it.
        # Matching rendered output alone must not make stale metadata acceptable.
        if result.profile_hash != snapshot.profile_hash:
            return self._handle_mismatch(
                "profile_hash", snapshot.profile_hash, result.profile_hash
            )
        if result.engine_version != snapshot.engine_version:
            return self._handle_mismatch(
                "engine_version", snapshot.engine_version, result.engine_version
            )

        nodes = render_models(result)
        actual_hash = AlignmentSnapshotValidator.compute_alignments_hash(nodes)
        if actual_hash != snapshot.output_alignments_hash:
            return self._handle_mismatch(
                "output_alignments_hash", snapshot.output_alignments_hash, actual_hash
            )
        return True
